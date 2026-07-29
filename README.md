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
2. Run it. A console window opens and stays open — that is the app.
3. Press play on anything, then open <http://127.0.0.1:5788/now-playing>.

Close the window to stop it.

Options: `NowPlayingBridge.exe --port 5789`, `--host 0.0.0.0`, `--quiet`.

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
    "thumbnail": "data:image/jpeg;base64,..."
  }
}
```

`session` is `null` when nothing is loaded or playback is fully stopped — that is
the overlay's cue to hide. A pause keeps the session and sets `status` to
`"paused"`.

`status` is one of `playing`, `paused`, `changing`, `stopped`, `opened`,
`closed`. `position_ms` is exact at `position_at` (epoch ms), so a client can
interpolate between polls without drifting.

`thumbnail` is a ready-to-use data URL, or `null` when the player supplies no
artwork.

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

## Notes

Windows hands over whichever app most recently played audio, so a YouTube tab can
take the spot from your music player. Use `?app=` to pin it.

Unsigned executables get a SmartScreen warning on first run ("More info" → "Run
anyway"). Building it yourself from source avoids that.

## License

GPL-3.0. See [LICENSE](LICENSE).

