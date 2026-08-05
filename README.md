# FlashRec

Hey y'all — since there are no good free recording softwares for timelapses, I decided to make one using Claude, and I figured it could be helpful to a lot of people.

A minimal screen recorder with a proper time lapse mode. It samples frames instead of recording everything and speeding it up afterwards, so a 10-hour session doesn't turn into a 40 GB file and doesn't melt your CPU.

## Download

**[⬇ FlashRec.exe (latest release)](https://github.com/albertocammilli/FlashRec/releases/tag/v1.0.0)**

No Python needed. Windows will show a SmartScreen warning because the exe isn't code-signed — click **More info → Run anyway**, or just build it yourself from source below.

## What it does

**Record** — 1:1 real time capture at 30 fps, with optional microphone and/or system audio. Audio is muxed into the file when you stop.

**Time lapse** — grabs one frame every `multiplier / 30` seconds. At 200x that's one frame every 6.7 seconds. The frames it skips are never captured and never encoded, which is the whole point: it stays light on the hardware and the file stays small. Speeds: 5x, 10x, 30x, 60x, 100x, 200x.

Everything is encoded live, so when you hit stop the file is already done.

## Other stuff it does

- Uses **ffmpeg / libx264** if it's on your PATH, falls back to OpenCV's `mp4v` writer if not
- Picks any display, or all of them at once on a multi-monitor setup
- Quality: full resolution, 75% or 50%
- 3 second countdown before it starts (click again to cancel)
- Floating timer pill in the corner — click it to stop
- **The app window and the timer never appear in the recording** (Windows `WDA_EXCLUDEFROMCAPTURE`)
- If your machine can't keep up in Record mode, it duplicates the last frame to keep video and audio in sync, and tells you afterwards if too many frames got repeated
- `Space` starts/stops, `Esc` closes

Files land in `%USERPROFILE%\Videos\FlashRec`. Click the path at the bottom of the window to open the folder.

## Run from source

```bash
pip install PyQt6 opencv-python mss numpy sounddevice
python flashrec.py
```

`sounddevice` is optional — without it the app runs fine, the audio options are just greyed out.

ffmpeg is also optional but recommended: without it you fall back to `mp4v` (bigger files, worse quality) **and audio muxing won't work at all**, since the merge step is done by ffmpeg.

## Build the exe yourself

```bash
pip install pyinstaller
pyinstaller --onefile --noconsole --name FlashRec flashrec.py
```

Output ends up in `dist/FlashRec.exe`.

## Requirements / caveats

- **Windows only in practice.** The core capture works anywhere, but hiding the app from the recording, system audio (WASAPI loopback) and the on-screen timer are all Windows-specific.
- Hiding windows from capture needs **Windows 10 build 2004 or newer**. On older builds the timer pill just doesn't show up.
- System audio is Windows-only. Mic capture works everywhere `sounddevice` does.
- Audio is **Record mode only** — a time lapse has no meaningful audio track.

## License

MIT.
