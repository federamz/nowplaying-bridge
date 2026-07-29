# NowPlaying Bridge

Exposes what Windows is playing as a small local JSON API, so a browser-based
stream overlay can show it.

Windows already tracks the current track — it is what puts the song name on your
keyboard's media keys. That system is called **SMTC** (System Media Transport
Controls), and nearly every player reports to it: Apple Music, Spotify, TIDAL,
Qobuz, Deezer, browsers, foobar2000, MusicBee, VLC, local files.

Run this, and your overlay can read all of them through one endpoint — no
per-service API keys, no scrobbling account, no waiting for a third party to
refresh. Real playback position, instant pause, artwork included.

Read-only. It never controls playback, and it only listens on localhost.

## Quick start

1. Download `NowPlayingBridge.exe` from the [latest release](../../releases/latest).
2. Run it. A small status window opens showing what it can see.
3. Press play on anything — the window names the track and the app it came from.

Close the window to stop it.

Options: `--port 5789`, `--host 0.0.0.0`, `--console` (no window, log to a
terminal instead), `--quiet`.

## Endpoints

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/now-playing` | The session your overlay should show, or `null`. |
| `GET` | `/now-playing?app=applemusic` | Same, but only from a matching app. Substring match on the app id or its friendly name. |
| `GET` | `/sessions` | Every session Windows currently reports, lightly described. |
| `GET` | `/health` | Whether the SMTC read is working. |

Every response sends `Access-Control-Allow-Origin: *`, so an overlay loaded from
`file://` or from a stream tool can fetch it.

### `/now-playing`

```json
{
  "bridge": "NowPlaying Bridge",
  "version": "1.0.0",
  "updated_at": 1785300000.123,
  "session": {
    "source": "AppleInc.AppleMusicWin_nzyj5cx40ttqa!App",
    "source_name": "Apple Music",
    "status": "playing",
    "is_playing": true,
    "title": "Fast Car",
    "artist": "Tracy Chapman",
    "album": "Tracy Chapman",
    "album_artist": "Tracy Chapman",
    "track_number": 1,
    "genres": [],
    "duration_ms": 296000,
    "position_ms": 63120,
    "position_at": 1785300000123,
    "position_snapshot_ms": 61000,
    "position_updated_at": 1785299998003,
    "timeline_ok": true,
    "thumbnail": "data:image/jpeg;base64,..."
  }
}
```

`session` is `null` when nothing is loaded or playback is fully stopped — that is
the overlay's cue to hide. A pause keeps the session and sets `status` to
`"paused"`.

`status` is one of `playing`, `paused`, `changing`, `stopped`, `opened`,
`closed`.

`thumbnail` is a ready-to-use data URL, or `null` when the player supplies no
artwork.

### Why position needs extrapolating

SMTC's `Position` is a **snapshot, not a running clock**. A player pushes it when
something changes — track start, seek, pause — then leaves it alone. Poll it
naively and the bar looks frozen, or snaps back to an old value every read.
`LastUpdatedTime` is the missing half: it says when that snapshot was taken, so
the true position while playing is `Position + (now − LastUpdatedTime)`.

This bridge does that for you:

| Field | Meaning |
| :--- | :--- |
| `position_ms` | **Live** position, recomputed on every request. |
| `position_at` | Epoch ms at which `position_ms` was true. Interpolate from here between polls. |
| `position_snapshot_ms` | The raw, un-extrapolated SMTC value, if you'd rather do the maths yourself. |
| `position_updated_at` | Epoch ms when Windows last received that snapshot. `0` means the player never sent one. |
| `timeline_ok` | `false` when there is no usable timeline — show an indeterminate bar, or none. |

Seeks land immediately, and pausing freezes the number, because a paused session
is never extrapolated. `playback_rate` is honoured when a player reports one.

## Building it yourself

Needs Windows and Python 3.10+.

```
git clone https://github.com/YOUR-USERNAME/nowplaying-bridge
cd nowplaying-bridge
build.bat
```

The exe lands in `dist\NowPlayingBridge.exe`. To run from source without
building: `pip install -r requirements.txt` then `python nowplaying_bridge.py`.

No Python? The **Actions** tab builds the exe on GitHub's Windows runners —
run the *Build NowPlayingBridge.exe* workflow and download the artifact.

`winotify` is optional — it only provides the "connected" toast. Everything works
without it.

`winsdk` is pinned to `1.0.0b10`: that is the newest release Microsoft's Python
projection ever published, and it is stable in practice despite the beta tag.

## Notes

Windows hands over whichever app most recently played audio, so a YouTube tab can
take the spot from your music player. Use `?app=` to pin it.

Windows also drops the session entirely for a beat when a player skips tracks.
The bridge holds the last good session for 4 seconds through that gap, so
overlays don't blink on every skip.

Unsigned executables get a SmartScreen warning on first run ("More info" → "Run
anyway"). Building it yourself from source avoids that.

## License

GPL-3.0. See [LICENSE](LICENSE).

## Credits

Written by Federico Ramirez Honack for
[Now Playing OS](https://github.com/YOUR-USERNAME). Independent implementation,
but the idea of putting SMTC behind a local API came from nutty's
[smtc-bridge](https://github.com/nuttylmao/smtc-bridge) — worth a look, and worth
a thank you.
