# ulePack

_What it does:_ Shrinks and converts screen recordings so they fit chat-app upload limits (Discord 10 MB, WhatsApp 16 MB, email, Slack…). It's an ffmpeg wrapper with a web UI, a Textual TUI and a scriptable CLI, all on one engine.
_Stack:_ Python 3.12, ffmpeg/ffprobe, Textual (TUI), stdlib HTTP server (web UI, the Local Browser UI Pattern)
_Next:_ Remember the last used preset in the TUI; a Wave widget like tidyUp's.

## Install

    uv tool install --force ~/repos/active/ulePack

Needs `ffmpeg` on PATH (`sudo apt install ffmpeg`).

## Use

    ulepack ui                        # web UI: drop files, pick a size, preview, download
    ulepack                           # TUI on ~/Videos/Screencasts (space/enter select · s shrink · g GIF · q quit)
    ulepack tui ~/Downloads           # TUI on another folder
    ulepack rec.webm                  # quality mode: small, sharp MP4 (CRF, ≤1080p, ≤30 fps)
    ulepack rec.webm -p discord       # guaranteed under 10 MB
    ulepack rec.mkv -t 8M --no-audio  # custom limit
    ulepack rec.mp4 --start 0:05 --end 0:35 --speed 2 -p whatsapp
    ulepack rec.webm --gif -t 5M      # GIF under 5 MB
    ulepack ~/Videos/Screencasts -p slack -d ~/share   # whole folder
    ulepack rec.webm -p discord -n    # dry run: print the plan only
    ulepack rec.webm -i               # file info
    ulepack presets                   # list the limits

Outputs go next to the input as `<name>.shrunk.mp4` (or `.gif`), never overwriting anything (`-2`, `-3`…).
`--json` prints a machine-readable summary on stdout.

## How it fits a size

- **Quality mode** (no limit): H.264 CRF 26, `preset slow`, capped at 1080p and 30 fps, `+faststart`, yuv420p.
  This mode plays inline in every chat app.
- **Size mode** (`-t`/`-p`): computes the bitrate budget from the duration (4% headroom), then picks the largest
  resolution and frame rate that stay readable (≥0.035 bits/pixel/frame for screen content), stepping down
  through 1440/1080/900/720/540/480… and 24/15/10 fps.
  - Roomy budget: first a CRF encode capped at that bitrate, so easy clips stay small instead of being padded up to the limit.
  - Tight budget, or the capped encode overshot: an exact two-pass encode. If the result is still over the limit, it retries with a lower bitrate, up to 3 times.
- **GIF**: palettegen/paletteuse (128 colours, bayer dither, diff rectangles). With a limit set, it steps the width
  down (800→640→480→360→240), then the fps.
- `--codec hevc` is about 30% smaller, `av1` smaller still, but both play in fewer apps. H.264 is the safe default.
- GNOME screencasts (VP8 WebM, variable frame rate, sometimes no duration) are handled: unknown fps → 30, and a missing duration is found by decoding.

## Web UI

`ulepack ui` serves `127.0.0.1:8766` (or the next free port) with a stable per-user token
(`~/.local/share/ulepack/ui-token`); `--no-open`, `--port`, `--url`, `--folder`, `--idle-timeout MIN`.

**Stopping (the port is freed every way):**
- **Stop** button in the page header. It asks first if jobs are running, cancels them (ffmpeg is killed and the
  partial file removed), and the server exits.
- **Idle auto-stop** after 5 minutes without use (`--idle-timeout 10`; `0` = never). The page's background
  polling doesn't count as use. Clicks, typing, scrolling, mouse movement over the page and API actions do.
  It never stops while a job is queued or running, and the countdown starts when the queue empties. In the
  last minute a bar shows the countdown with **Keep running**. Closing the tab simply lets it idle out.
- Ctrl+C in its terminal (also cancels jobs).

After a stop, the page shows a "stopped" screen and reconnects by itself when you run `ulepack ui` again. It follows
[[Local Browser UI Pattern]]: Host-header guard, a token on every API call, polling with `204` when nothing changed,
and no-store. A newer install replaces an older running server automatically.

- Folder browser with thumbnails (cached in `~/.cache/ulepack/thumbs`). Quick links to Screencasts, Videos, Downloads, Desktop.
- Drag and drop anywhere on the page. Dropped files are copied to `~/Videos/ulePack/`, and the outputs go next to them.
- Preset chips and a custom MB limit, MP4/GIF, and more options (codec, max height, fps, speed, trim, drop audio).
  The page shows a live plan for the selected file.
- Job queue with live progress and cancel. For finished jobs: **Preview** (in-page player), **Download** (or drag the
  button to the desktop), **Copy file** (puts the file on the clipboard via `xclip` to paste into Files or a chat app),
  **Copy path**, and **Show in folder**.

## Layout

    src/ulepack/core.py        engine: probe, plan, encode (shared by everything)
    src/ulepack/jobs.py        single-worker job queue (web UI + TUI)
    src/ulepack/cli.py         argparse CLI + `ui` / `tui` subcommands
    src/ulepack/tui.py         Textual app (git-odos theme, same tokens as tidyUp)

_Name:_ Albanian "ule pak", "lower it a bit", spelled ulePack (pak ≈ pack).
    src/ulepack/web/server.py  stdlib HTTP server + JSON API
    src/ulepack/web/static/    index.html, app.js, style.css, theme.js, icon.svg

Claude Code knows this tool through the skill `~/.claude/skills/ulepack/SKILL.md`.
