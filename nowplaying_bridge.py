"""
NowPlaying Bridge — exposes Windows "now playing" media info as a local JSON API.

Windows already knows what your PC is playing: it is what puts the track name on
your keyboard's media keys and in the volume flyout. That system is called SMTC
(System Media Transport Controls), and every well-behaved player reports to it —
Apple Music, Spotify, TIDAL, Qobuz, browsers, local file players.

This program reads SMTC and serves it on http://127.0.0.1:5788/now-playing so a
browser-based stream overlay can display it. Read-only: it never controls
playback and never touches the network beyond localhost.

Copyright (C) 2026 Federico Ramirez Honack

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. This program is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
details. You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

import argparse
import asyncio
import base64
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

APP_NAME = "NowPlaying Bridge"
VERSION = "1.3.0"
DEFAULT_PORT = 5788
POLL_SECONDS = 0.25
QUIET = False

# --------------------------------------------------------------------------
# Shared state. The poller thread writes it, the HTTP threads read it.
# A plain dict swap under a lock is enough — no partial reads that way.
# --------------------------------------------------------------------------
_state_lock = threading.Lock()
_state = {"sessions": [], "current": None, "updated_at": 0.0, "error": None}

# Base64 artwork is expensive to build, so keep the last one per track.
_thumb_cache = {}
_THUMB_CACHE_MAX = 8


def set_state(**kwargs):
    with _state_lock:
        _state.update(kwargs)


def get_state():
    with _state_lock:
        return dict(_state)


def safe_print(*parts):
    """
    Windows consoles are not always UTF-8, and track names are not always ASCII.
    In windowed builds there is no stdout at all, so this must never raise.
    """
    if not getattr(sys, "stdout", None):
        return
    text = " ".join(str(p) for p in parts)
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        print(text.encode(enc, "replace").decode(enc, "replace"), flush=True)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Friendly app names. SMTC identifies apps by their package or exe id, which
# looks like "AppleInc.AppleMusicWin_nzyj5cx40ttqa!App" — not something to put
# on a stream. Substring match, first hit wins.
# --------------------------------------------------------------------------
FRIENDLY_NAMES = [
    ("applemusic", "Apple Music"),
    ("apple.music", "Apple Music"),
    ("itunes", "iTunes"),
    ("spotify", "Spotify"),
    ("tidal", "TIDAL"),
    ("qobuz", "Qobuz"),
    ("deezer", "Deezer"),
    ("soundcloud", "SoundCloud"),
    ("zunemusic", "Groove"),
    ("msedge", "Microsoft Edge"),
    ("chrome", "Google Chrome"),
    ("firefox", "Firefox"),
    ("brave", "Brave"),
    ("opera", "Opera"),
    ("vivaldi", "Vivaldi"),
    ("vlc", "VLC"),
    ("mpv", "mpv"),
    ("foobar2000", "foobar2000"),
    ("musicbee", "MusicBee"),
    ("aimp", "AIMP"),
    ("winamp", "Winamp"),
    ("audacious", "Audacious"),
]


def friendly_name(source_id):
    if not source_id:
        return ""
    low = source_id.lower()
    for needle, label in FRIENDLY_NAMES:
        if needle in low:
            return label
    # Fall back to something readable: "Some.Publisher.AppName_hash!App" -> "AppName"
    head = source_id.split("!")[0].split("_")[0]
    tail = head.split(".")[-1] or source_id
    return tail[:-4] if tail.lower().endswith(".exe") else tail


STATUS_NAMES = {
    0: "closed",
    1: "opened",
    2: "changing",
    3: "stopped",
    4: "playing",
    5: "paused",
}


def ms(timespan):
    """winsdk maps Windows TimeSpan to datetime.timedelta."""
    if timespan is None:
        return 0
    try:
        return int(timespan.total_seconds() * 1000)
    except AttributeError:
        return 0


def epoch_ms(dt):
    """winsdk maps Windows DateTime to an aware datetime.datetime."""
    if dt is None:
        return 0
    try:
        value = int(dt.timestamp() * 1000)
    except (AttributeError, OSError, OverflowError, ValueError):
        return 0
    # Windows reports 1601-01-01 (or 1970) when a player has never pushed a
    # timeline at all. Treat anything implausible as "no timeline".
    return value if value > 0 else 0


async def read_thumbnail(ref):
    """
    SMTC hands artwork over as a stream reference, not bytes. Different winsdk
    versions expose the buffer differently, so try the two known paths and give
    up quietly — a missing cover is not worth failing a request over.
    """
    from winsdk.windows.storage.streams import Buffer, DataReader, InputStreamOptions

    stream = await ref.open_read_async()
    size = stream.size
    if not size:
        return None
    buf = Buffer(size)
    await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
    try:
        return bytes(memoryview(buf))
    except Exception:
        reader = DataReader.from_buffer(buf)
        raw = bytearray(buf.length)
        reader.read_bytes(raw)
        return bytes(raw)


async def describe(session):
    """Turn one SMTC session into the flat dict the API serves."""
    props = await session.try_get_media_properties_async()
    info = session.get_playback_info()
    timeline = session.get_timeline_properties()

    source = session.source_app_user_model_id or ""
    title = (props.title or "").strip()
    artist = (props.artist or "").strip()

    status_value = int(info.playback_status) if info.playback_status is not None else 0
    start = ms(timeline.start_time)
    duration = max(0, ms(timeline.end_time) - start)
    snapshot = max(0, ms(timeline.position) - start)
    updated_at = epoch_ms(timeline.last_updated_time)

    # Position is a SNAPSHOT, not a running clock: players only push it when
    # something changes, so polling it straight looks frozen (or snaps back to an
    # old value). LastUpdatedTime says when the snapshot was taken, so the real
    # position while playing is snapshot + time elapsed since then.
    now_ms = int(time.time() * 1000)
    timeline_ok = duration > 0 and updated_at > 0
    position = snapshot
    if timeline_ok and status_value == 4:
        rate = 1.0
        try:
            if info.playback_rate:
                rate = float(info.playback_rate) or 1.0
        except (TypeError, ValueError):
            rate = 1.0
        position = snapshot + int(max(0, now_ms - updated_at) * rate)
    if duration and position > duration:
        position = duration

    thumbnail = None
    if props.thumbnail is not None:
        key = (source, title, artist)
        if key in _thumb_cache:
            thumbnail = _thumb_cache[key]
        else:
            try:
                raw = await read_thumbnail(props.thumbnail)
                if raw:
                    thumbnail = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
            except Exception:
                thumbnail = None
            if len(_thumb_cache) >= _THUMB_CACHE_MAX:
                _thumb_cache.clear()
            _thumb_cache[key] = thumbnail

    genres = []
    try:
        genres = [g for g in (props.genres or [])]
    except Exception:
        pass

    return {
        "source": source,
        "source_name": friendly_name(source),
        "status": STATUS_NAMES.get(status_value, "unknown"),
        "is_playing": status_value == 4,
        "title": title,
        "artist": artist,
        "album": (props.album_title or "").strip(),
        "album_artist": (props.album_artist or "").strip(),
        "track_number": int(props.track_number or 0),
        "genres": genres,
        "duration_ms": duration,
        "position_ms": position,
        "position_at": now_ms,
        "position_snapshot_ms": snapshot,
        "position_updated_at": updated_at,
        "timeline_ok": timeline_ok,
        "thumbnail": thumbnail,
    }


def pick_current(sessions, current_source):
    """
    The session a widget should show when no app filter is set.

    A session that is actually playing wins over whatever Windows calls the
    "current" one: pause a YouTube tab and start Apple Music, and Windows often
    keeps pointing at the browser, which would freeze the overlay on the old
    track. Preferring the playing session is what a viewer expects.
    """
    playing = [s for s in sessions if s["is_playing"] and s["title"]]
    if playing:
        # More than one thing playing at once: honour the system's pick among them.
        for s in playing:
            if s["source"] == current_source:
                return s
        return playing[0]
    if current_source:
        for s in sessions:
            if s["source"] == current_source:
                return s
    return sessions[0] if sessions else None


def is_showable(session):
    """Nothing loaded, or fully stopped, means the overlay should hide."""
    if not session or not session["title"]:
        return False
    return session["status"] not in ("closed", "opened", "stopped", "unknown")


def live_session(session):
    """
    Re-extrapolate position at serve time.

    The poller only refreshes every POLL_SECONDS, so its position can be a
    fraction of a second stale by the time a request arrives. Recomputing from
    position_updated_at here means every response is current.
    """
    if not session:
        return None
    out = dict(session)
    now_ms = int(time.time() * 1000)
    out["position_at"] = now_ms
    if out.get("timeline_ok") and out.get("is_playing"):
        pos = out["position_snapshot_ms"] + max(0, now_ms - out["position_updated_at"])
        dur = out.get("duration_ms") or 0
        out["position_ms"] = min(pos, dur) if dur else pos
    return out


# Windows drops the session entirely for a beat when a player skips tracks or
# swaps queues. Holding the last good session through that gap keeps overlays
# from blinking out on every skip.
GRACE_MS = 4000
_last_good = None


async def poll_forever():
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    )

    manager = None
    announced = False
    last_key = None

    while True:
        try:
            if manager is None:
                manager = await SessionManager.request_async()

            described = []
            for session in manager.get_sessions():
                try:
                    described.append(await describe(session))
                except Exception:
                    continue  # one flaky player must not blank the whole API

            current_source = ""
            try:
                cur = manager.get_current_session()
                if cur is not None:
                    current_source = cur.source_app_user_model_id or ""
            except Exception:
                pass

            current = pick_current(described, current_source)

            global _last_good
            now_wall = time.time()
            if is_showable(current):
                _last_good = {"session": current, "at": now_wall}
            elif _last_good and (now_wall - _last_good["at"]) * 1000 < GRACE_MS:
                current = _last_good["session"]
                # Keep the held session findable by ?app= filters too, or a
                # pinned widget would still see the skip-gap.
                if current["source"] not in {s["source"] for s in described}:
                    described = described + [current]

            set_state(
                sessions=described,
                current=current,
                updated_at=time.time(),
                error=None,
            )

            if is_showable(current):
                key = (current["source"], current["title"], current["artist"])
                if key != last_key:
                    last_key = key
                    label = current["source_name"] or "your player"
                    if not QUIET:
                        safe_print(f"  now playing  {current['title']} — {current['artist']}  [{label}]")
                if not announced:
                    announced = True
                    notify("Connected", f"Reading {current['source_name'] or 'your player'}")
        except Exception as exc:
            manager = None
            set_state(error=str(exc), updated_at=time.time())

        await asyncio.sleep(POLL_SECONDS)


def notify(title, message):
    """
    A Windows toast, if the optional dependency is present. Purely cosmetic —
    the bridge works exactly the same without it.
    """
    try:
        from winotify import Notification

        toast = Notification(app_id=APP_NAME, title=f"{APP_NAME} — {title}", msg=message)
        toast.show()
    except Exception:
        pass


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME.replace(' ', '')}/{VERSION}"

    def log_message(self, *args):
        pass  # a polling widget would flood the console

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Overlays are loaded from file:// or from a stream tool, so their origin
        # is opaque. Localhost-only data, so a wildcard is safe here.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        state = get_state()

        if route in ("/", "/now-playing"):
            wanted = (query.get("app") or [""])[0].strip().lower()
            session = state["current"]
            if wanted:
                matches = [
                    s for s in state["sessions"]
                    if wanted in f"{s['source']} {s['source_name']}".lower()
                ]
                # Same rule as pick_current: a playing match beats a paused one.
                session = next((s for s in matches if s["is_playing"]), None) or (
                    matches[0] if matches else None
                )
            self._send({
                "bridge": APP_NAME,
                "version": VERSION,
                "updated_at": state["updated_at"],
                "session": live_session(session) if is_showable(session) else None,
            })
            return

        if route == "/sessions":
            self._send({
                "bridge": APP_NAME,
                "version": VERSION,
                "sessions": [
                    {
                        "source": s["source"],
                        "source_name": s["source_name"],
                        "status": s["status"],
                        "title": s["title"],
                        "artist": s["artist"],
                    }
                    for s in state["sessions"]
                ],
            })
            return

        if route == "/health":
            self._send({
                "bridge": APP_NAME,
                "version": VERSION,
                "ok": state["error"] is None,
                "error": state["error"],
                "session_count": len(state["sessions"]),
                "updated_at": state["updated_at"],
            })
            return

        self._send({"error": "not found", "endpoints": ["/now-playing", "/sessions", "/health"]}, 404)


def main():
    parser = argparse.ArgumentParser(description=f"{APP_NAME} {VERSION}")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"default {DEFAULT_PORT}")
    parser.add_argument("--host", default="127.0.0.1", help="default 127.0.0.1 (this PC only)")
    parser.add_argument("--console", action="store_true", help="no window, log to the console")
    parser.add_argument("--quiet", action="store_true", help="don't print track changes")
    args = parser.parse_args()

    if sys.platform != "win32":
        safe_print(f"{APP_NAME} needs Windows — SMTC is a Windows API.")
        return 1

    if args.quiet:
        global QUIET
        QUIET = True

    poller = threading.Thread(
        target=lambda: asyncio.run(poll_forever()), name="smtc-poller", daemon=True
    )
    poller.start()

    url = f"http://{args.host}:{args.port}/now-playing"

    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        msg = (
            f"Could not start on port {args.port}.\n\n{exc}\n\n"
            f"Something else may be using it. Try:\n"
            f"NowPlayingBridge.exe --port 5789"
        )
        if args.console:
            safe_print("  " + msg.replace("\n", "\n  "))
            input("\n  Press Enter to close.")
        else:
            show_error(f"{APP_NAME} — could not start", msg)
        return 1

    serve = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    serve.start()
    notify("Running", "Leave the window open while you stream.")

    if args.console:
        safe_print("")
        safe_print(f"  {APP_NAME} {VERSION}")
        safe_print(f"  {url}")
        safe_print("")
        safe_print("  Leave this window open while you stream. Close it to stop.")
        safe_print("")
        try:
            serve.join()
        except KeyboardInterrupt:
            safe_print("\n  Stopped.")
        finally:
            server.server_close()
        return 0

    # Windowed mode is the default: a console is fine for developers but reads as
    # something gone wrong to everyone else.
    try:
        from status_window import StatusWindow

        StatusWindow(APP_NAME, VERSION, url, get_state, server.shutdown).run()
    except Exception as exc:
        # Never strand the user with no UI and no explanation — fall back to console.
        safe_print(f"  Could not open the window ({exc}). Running in console mode.")
        safe_print(f"  {url}")
        try:
            serve.join()
        except KeyboardInterrupt:
            pass
    finally:
        server.server_close()
    return 0


def show_error(title, message):
    """A message box, so a startup failure isn't invisible in windowed mode."""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        safe_print(f"{title}: {message}")


if __name__ == "__main__":
    sys.exit(main())
