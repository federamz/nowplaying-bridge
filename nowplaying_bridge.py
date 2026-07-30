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
import configparser
import json
import os
import socket
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

APP_NAME = "NowPlaying Bridge"
VERSION = "1.6.0"
DEFAULT_PORT = 5788
POLL_SECONDS = 0.25
QUIET = False


# --------------------------------------------------------------------------
# Living next to the exe: settings, crash logs, one-instance-only.
#
# None of this is about SMTC. It is about the app being supportable once it is
# on someone else's PC, where there is no console to read and no way to ask them
# to pass a command-line flag.
# --------------------------------------------------------------------------
def app_dir():
    """The folder the exe sits in (not PyInstaller's temp unpack folder)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


SETTINGS_TEMPLATE = """; NowPlaying Bridge settings
;
; port  which port the bridge serves on. Change it only if another program
;       already uses 5788 (the app will tell you if that happens).
; host  127.0.0.1 means this PC only, which is what you want. Set 0.0.0.0
;       only if you run OBS on a different machine on your network.

[server]
host = 127.0.0.1
port = 5788
"""


def load_settings():
    """
    Read settings.ini from beside the exe, writing a commented default if absent.

    A file a buyer can open in Notepad beats a command-line flag they will never
    type. Command-line flags still win over the file when both are given.
    """
    path = os.path.join(app_dir(), "settings.ini")
    parser = configparser.ConfigParser()
    parser.read_string(SETTINGS_TEMPLATE)
    if os.path.exists(path):
        try:
            parser.read(path, encoding="utf-8")
        except Exception:
            pass  # a mangled file should never stop the app starting
    else:
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(SETTINGS_TEMPLATE)
        except OSError:
            pass  # read-only folder (Program Files); defaults still apply
    host = parser.get("server", "host", fallback="127.0.0.1").strip() or "127.0.0.1"
    try:
        port = parser.getint("server", "port", fallback=DEFAULT_PORT)
    except ValueError:
        port = DEFAULT_PORT
    if not 1 <= port <= 65535:
        port = DEFAULT_PORT
    return host, port


def log_crash(exc):
    """
    Write a timestamped crash log beside the exe and keep the newest 10.

    In a windowed build a traceback has nowhere to go, so a crash would be
    invisible: the app simply vanishes. A file turns "it stopped working" into
    something a buyer can send and I can read.
    """
    try:
        folder = os.path.join(app_dir(), "logs")
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        with open(os.path.join(folder, f"crash {stamp}.txt"), "w", encoding="utf-8") as handle:
            handle.write(f"{APP_NAME} {VERSION}\n")
            handle.write(f"Time: {stamp}\n")
            handle.write(f"Python: {sys.version}\n\n")
            handle.write(f"{exc}\n\n")
            handle.write(traceback.format_exc())
        logs = sorted(
            (os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".txt")),
            key=os.path.getmtime,
        )
        for stale in logs[:-10]:
            try:
                os.remove(stale)
            except OSError:
                pass
    except Exception:
        pass  # logging a crash must never cause one


def already_running(host, port):
    """
    True when another copy of this bridge already holds the port.

    Asking the port directly beats a PID lock file: no stale locks to clean up
    after a crash, and it distinguishes our own bridge from some unrelated
    program that happens to use the same port.
    """
    probe = "127.0.0.1" if host in ("0.0.0.0", "") else host
    try:
        with socket.create_connection((probe, port), timeout=0.6) as sock:
            sock.sendall(
                f"GET /health HTTP/1.0\r\nHost: {probe}\r\nConnection: close\r\n\r\n".encode()
            )
            reply = b""
            while len(reply) < 4096:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                reply += chunk
    except OSError:
        return False
    return APP_NAME.encode() in reply

# --------------------------------------------------------------------------
# Shared state. The poller thread writes it, the HTTP threads read it.
# A plain dict swap under a lock is enough — no partial reads that way.
# --------------------------------------------------------------------------
_state_lock = threading.Lock()
_state = {"sessions": [], "current": None, "updated_at": 0.0, "error": None}

# Base64 artwork is expensive to build, so keep the last one per track.
#
# Cached entries stay open to revision for ART_SETTLE_MS. Players push metadata
# out of order — Apple Music web reports the new title while its thumbnail is
# still the previous track's — so the first read after a track change can be the
# wrong image. Re-reading during the settle window catches the real artwork when
# it lands, and clients swap it in place.
_thumb_cache = {}
_THUMB_CACHE_MAX = 8
ART_SETTLE_MS = 6000


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

# Windows classifies each session, which is how a widget can follow music and
# ignore a YouTube clip or a Twitch stream in the same browser.
PLAYBACK_TYPES = {
    0: "unknown",
    1: "music",
    2: "video",
    3: "image",
}

REPEAT_MODES = {
    0: "none",
    1: "track",
    2: "list",
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

    # Extra SMTC fields. Every one is optional per player, so each read is
    # defended individually rather than in one try block that would drop them all.
    def opt(read, cast, fallback):
        try:
            value = read()
            return fallback if value is None else cast(value)
        except Exception:
            return fallback

    type_value = opt(lambda: info.playback_type, int, 0)
    repeat_value = opt(lambda: info.auto_repeat_mode, int, 0)
    shuffle = opt(lambda: info.is_shuffle_active, bool, False)
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
        entry = _thumb_cache.get(key)
        settled = entry is not None and (now_ms - entry["first"]) > ART_SETTLE_MS
        if settled:
            thumbnail = entry["data"]
        else:
            try:
                raw = await read_thumbnail(props.thumbnail)
                if raw:
                    thumbnail = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
            except Exception:
                thumbnail = None
            if entry is None:
                if len(_thumb_cache) >= _THUMB_CACHE_MAX:
                    _thumb_cache.clear()
                _thumb_cache[key] = {"data": thumbnail, "first": now_ms}
            elif thumbnail:
                # Keep the original timestamp: the window is per track, not per read.
                entry["data"] = thumbnail
            else:
                # A momentary read failure must not blank art we already have.
                thumbnail = entry["data"]

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
        # A widget can use this to ignore video sessions (a YouTube clip or a
        # Twitch stream) and follow music only.
        "playback_type": PLAYBACK_TYPES.get(type_value, "unknown"),
        "is_shuffle_active": shuffle,
        "auto_repeat_mode": REPEAT_MODES.get(repeat_value, "none"),
        "title": title,
        "artist": artist,
        "album": (props.album_title or "").strip(),
        "album_artist": (props.album_artist or "").strip(),
        "track_number": int(props.track_number or 0),
        "album_track_count": opt(lambda: props.album_track_count, int, 0),
        "subtitle": opt(lambda: props.subtitle, lambda v: str(v).strip(), ""),
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

    def _send_sessions_page(self, state):
        """A plain readable list of what Windows can see right now."""
        def esc(text):
            return (
                str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )

        rows = []
        for s in state["sessions"]:
            playing = "playing" if s["is_playing"] else s["status"]
            track = " — ".join(x for x in (s["artist"], s["title"]) if x) or "nothing loaded"
            rows.append(
                "<li><code>{sid}</code><div class=meta>{name} · {kind} · {state}</div>"
                "<div class=track>{track}</div></li>".format(
                    sid=esc(s["source"]),
                    name=esc(s["source_name"]),
                    kind=esc(s["playback_type"]),
                    state=esc(playing),
                    track=esc(track),
                )
            )
        if not rows:
            rows.append(
                "<li class=empty>Nothing is reporting to Windows right now. "
                "Press play in a music app and reload this page.</li>"
            )
        body = """<!doctype html><html><head><meta charset=utf-8>
<title>{app} — active sessions</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>
body{{margin:0;background:#0e0e12;color:#f2f2f5;font:15px/1.5 "Segoe UI",system-ui,sans-serif;padding:32px}}
h1{{font-size:19px;margin:0 0 4px}}
p{{color:#8b8b97;margin:0 0 22px;font-size:13.5px;max-width:60ch}}
ul{{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:10px;max-width:70ch}}
li{{background:#16161c;border:1px solid #26262e;border-radius:12px;padding:14px 16px}}
li.empty{{color:#8b8b97}}
code{{font:13px/1.5 Consolas,monospace;color:#ffd479;word-break:break-all}}
.meta{{color:#8b8b97;font-size:12px;margin-top:6px;text-transform:uppercase;letter-spacing:.06em}}
.track{{margin-top:6px;font-size:14px}}
footer{{color:#8b8b97;font-size:12.5px;margin-top:24px}}
</style></head><body>
<h1>Active sessions</h1>
<p>Everything Windows can currently see. Copy the highlighted id into the
widget's “only follow this app” field to pin the overlay to one player.</p>
<ul>{rows}</ul>
<footer>{app} {ver} · reload to refresh</footer>
</body></html>""".format(app=APP_NAME, ver=VERSION, rows="".join(rows))
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

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
            # A streamer reading this page needs the app id with their eyes, to
            # paste into the widget's "only follow this app" field. Browsers get
            # a readable page; anything else (a script, curl) still gets JSON.
            wants_html = "text/html" in (self.headers.get("Accept") or "")
            if wants_html:
                self._send_sessions_page(state)
                return
            self._send({
                "bridge": APP_NAME,
                "version": VERSION,
                "sessions": [
                    {
                        "source": s["source"],
                        "source_name": s["source_name"],
                        "status": s["status"],
                        "playback_type": s["playback_type"],
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
    ini_host, ini_port = load_settings()
    parser = argparse.ArgumentParser(description=f"{APP_NAME} {VERSION}")
    parser.add_argument("--port", type=int, default=None, help=f"overrides settings.ini (default {ini_port})")
    parser.add_argument("--host", default=None, help=f"overrides settings.ini (default {ini_host})")
    parser.add_argument("--console", action="store_true", help="no window, log to the console")
    parser.add_argument("--quiet", action="store_true", help="don't print track changes")
    args = parser.parse_args()
    args.host = args.host or ini_host
    args.port = args.port or ini_port

    if sys.platform != "win32":
        safe_print(f"{APP_NAME} needs Windows — SMTC is a Windows API.")
        return 1

    # Double-clicking the exe twice is common. Say so plainly instead of dying
    # on a port-in-use error that reads like a fault.
    if already_running(args.host, args.port):
        msg = (
            f"{APP_NAME} is already running.\n\n"
            "Look for its window, or its icon in the taskbar. "
            "You only need one copy open."
        )
        if args.console:
            safe_print("  " + msg.replace("\n", "\n  "))
        else:
            show_error(APP_NAME, msg)
        return 0

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
            f"Another program is using it. Open settings.ini next to this app "
            f"and change the port to 5789, then start it again."
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
    # A windowed build has no console, so an unhandled exception would make the
    # app vanish with no trace. Log it where a buyer can find and send it.
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - last resort before exit
        log_crash(exc)
        show_error(
            f"{APP_NAME} stopped",
            "Something went wrong and the bridge had to close.\n\n"
            "A file was saved in the \"logs\" folder next to the app. "
            "Send me the newest one and I'll fix it.\n\n"
            f"{exc}",
        )
        sys.exit(1)
