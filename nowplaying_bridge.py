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
VERSION = "1.0.0"
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
    """Windows consoles are not always UTF-8, and track names are not always ASCII."""
    text = " ".join(str(p) for p in parts)
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        print(text.encode(enc, "replace").decode(enc, "replace"), flush=True)


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
    position = max(0, ms(timeline.position) - start)
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
        "position_at": int(time.time() * 1000),
        "thumbnail": thumbnail,
    }


def pick_current(sessions, current_source):
    """The session a widget should show when no app filter is set."""
    if current_source:
        for s in sessions:
            if s["source"] == current_source:
                return s
    for s in sessions:
        if s["is_playing"]:
            return s
    return sessions[0] if sessions else None


def is_showable(session):
    """Nothing loaded, or fully stopped, means the overlay should hide."""
    if not session or not session["title"]:
        return False
    return session["status"] not in ("closed", "opened", "stopped", "unknown")


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
                session = None
                for s in state["sessions"]:
                    haystack = f"{s['source']} {s['source_name']}".lower()
                    if wanted in haystack:
                        session = s
                        break
            self._send({
                "bridge": APP_NAME,
                "version": VERSION,
                "updated_at": state["updated_at"],
                "session": session if is_showable(session) else None,
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
    safe_print("")
    safe_print(f"  {APP_NAME} {VERSION}")
    safe_print(f"  {url}")
    safe_print("")
    safe_print("  Leave this window open while you stream. Close it to stop.")
    safe_print("")
    notify("Running", "Leave the window open while you stream.")

    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        safe_print(f"  Could not start on port {args.port}: {exc}")
        safe_print(f"  Something else may be using it. Try:  NowPlayingBridge.exe --port 5789")
        input("\n  Press Enter to close.")
        return 1

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        safe_print("\n  Stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
