# GoPro low-latency webcam (Windows)

A replacement for GoPro's Webcam desktop app that is **faster, looks better and runs itself**.
It talks to the camera directly over USB, decodes the stream with zero buffering and feeds it into
**OBS Virtual Camera**, so Discord, Zoom, Teams, Meet and OBS see it as a normal webcam.

| | GoPro Webcam app | This |
|---|---|---|
| Delay | ~0.5–1 s | ~0.2–0.35 s (camera encoder is the floor) |
| Colours | — | Full-range → limited-range done correctly (no crushed blacks) |
| Low light | — | Optional denoise + shadow lift + sharpen (~6 ms/frame) |
| Camera off / unplugged | App shows an error | "Waiting for GoPro" screen, live picture returns automatically |
| Stream freezes | Restart by hand | Detected and restarted in ~3 s |
| Starting it | Open app, pick camera | Starts at login, sits in the tray |

> Not affiliated with or endorsed by GoPro. "GoPro" and "HERO" are trademarks of GoPro, Inc.
> This project only uses the publicly documented [Open GoPro](https://gopro.github.io/OpenGoPro/) HTTP API.

## Supported cameras

- **Tested:** HERO11 Black
- **Should work:** HERO9, HERO10, HERO12, HERO13 Black (same Open GoPro webcam API) — please open an issue to confirm
- **Not supported:** HERO8 and older (different, older API)

Windows 10 or 11 is required. Update the camera to the latest firmware with the GoPro Quik app first.

## Install

1. Download this repo (**Code → Download ZIP**) and unzip it somewhere permanent, e.g. `Documents\gopro-webcam`.
2. Open PowerShell in that folder and run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File install.ps1
   ```

   It installs whatever is missing — [ffmpeg](https://ffmpeg.org/), Python 3.12 and
   [OBS Studio](https://obsproject.com/) (only for its virtual camera) — via `winget`, sets up a private
   Python environment in the folder, adds Startup + Desktop shortcuts and starts the webcam.
   Options: `-NoStartup`, `-NoDesktopShortcut`, `-NoLaunch`.

3. **Fully quit** Discord / Zoom / Teams once (tray icon → Quit) and reopen it, so it notices the new camera.
4. Pick **OBS Virtual Camera** as your camera (not "GoPro Webcam").
5. Plug the GoPro in with a USB-C **data** cable and turn it on. The picture appears within a few seconds.

## Using it

A camera icon sits in the system tray — **blue** when live, **grey** while waiting for the GoPro.
Right-click it for the status, **Restart stream**, **Open log** and **Quit**.

### Options

Edit the shortcut's target (Startup folder: `Win+R` → `shell:startup`) and add any of:

| Option | Values | Default |
|---|---|---|
| `--res` | `480`, `720`, `1080` | `1080` (the camera's maximum in webcam mode) |
| `--fov` | `wide`, `linear`, `narrow`, `superview` | `linear` (no fisheye) |
| `--look` | `natural`, `enhanced`, `bright` | `enhanced` |

- `natural` — the camera's picture, colour-range corrected only
- `enhanced` — light denoise, shadow lift, mild sharpening
- `bright` — stronger version for very dark rooms

To see logs live in a console instead of the tray, quit the tray copy and run `run-console.bat`.

## Troubleshooting

**The official GoPro Webcam app keeps reopening.** An app (usually Discord) still has *GoPro Webcam*
selected as its camera; opening that device launches GoPro's app. Switch it to *OBS Virtual Camera*.
This tool also closes the official app automatically when it sees it. You can stop it starting with
Windows in Task Manager → Startup apps.

**OBS fails to install with "files ... are currently in use".** Something has OBS's game-capture hook
loaded — normally Steam or a running game. Quit Steam completely and run the installer again.

**Discord shows nothing / camera won't open.** Make sure this tool is running (tray icon) and restart
Discord fully. While nothing feeds OBS Virtual Camera it advertises whatever resolution your OBS profile
uses (often 4K), which some apps reject; this tool keeps the camera open at 1080p all the time so that
doesn't happen.

**I want to use OBS's own virtual camera.** Only one program can drive it at a time — Quit this from the
tray first.

**It's 25 fps, not 30.** The camera's *Anti-Flicker* setting is 50 Hz. Set it to 60 Hz on the camera for
30 fps — but if your mains power is 50 Hz, indoor lights may flicker on camera.

**The picture is grainy.** That's the small sensor in a dark room. More light on your face helps far more
than any setting; `--look bright` helps a bit. Also note Discord re-encodes cameras to 720p without Nitro.

## How it works

```
GoPro ──USB (network adapter)──► HTTP :8080  /gopro/webcam/start?res=…&fov=…&port=8554
      ──UDP :8554  MPEG-TS / H.264 1080p ~6 Mbps──► ffmpeg (nobuffer, low_delay, no frame dup)
          ──► denoise / eq / sharpen ──► full→limited range NV12 ──► pyvirtualcam ──► OBS Virtual Camera
```

Details that matter:

- **ffmpeg must be listening before the stream starts.** The camera only sends the H.264 SPS/PPS headers
  at stream start; join late and the decoder can wait a minute or more. The tool always (re)starts the
  stream after ffmpeg is up.
- **Only the newest frame is kept**, so delay can never pile up.
- **The GoPro sends full-range (`yuvj420p`) video**; webcam consumers assume limited range, so it is
  converted explicitly.
- A watchdog restarts everything if no frame arrives for 6 seconds, and keep-alives stop the camera
  from sleeping.

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File uninstall.ps1 -RemoveEnvironment
```

Then delete the folder. ffmpeg, Python and OBS stay installed; remove them in Settings → Apps if you
don't need them.

## Licence

[MIT](LICENSE)
