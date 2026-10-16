"""ulepack command line.

  ulepack                         Textual TUI on ~/Videos/Screencasts (or the current folder)
  ulepack tui [FOLDER]            same, on FOLDER
  ulepack ui [--port N]           local web UI (127.0.0.1 only)
  ulepack presets                 list chat-app size limits
  ulepack FILE|FOLDER... [opts]   encode right away (scriptable; what Claude uses)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, core

DEFAULT_PORT = 8766  # tidyUp has 8765


def default_folder() -> Path:
    sc = Path.home() / "Videos" / "Screencasts"
    return sc if sc.is_dir() else Path.cwd()


def _size(text: str) -> int:
    try:
        return core.parse_size(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def _time(text: str) -> float:
    try:
        return core.parse_time(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"bad time: {text!r} (try 90, 1:30)") from e


def encode_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ulepack",
        description="Shrink/convert screen recordings to fit chat-app upload limits (ffmpeg).",
        epilog="examples:\n"
               "  ulepack rec.webm                      # small, sharp MP4 (1080p/30fps cap)\n"
               "  ulepack rec.webm -p discord           # guaranteed under 10 MB\n"
               "  ulepack rec.mkv -t 8M --no-audio      # custom size limit\n"
               "  ulepack rec.mp4 --start 0:05 --end 0:35 -p whatsapp\n"
               "  ulepack rec.webm --gif -t 5M          # GIF under 5 MB\n"
               "  ulepack ~/Videos/Screencasts -p slack -d ~/share\n"
               "\nother commands:\n"
               "  ulepack                 terminal UI (Textual)\n"
               "  ulepack tui [FOLDER]    terminal UI on a folder\n"
               "  ulepack ui              web UI in the browser (--port, --no-open)\n"
               "  ulepack presets         list platform limits\n"
               f"\npresets: {', '.join(f'{k}={v:g}MB' for k, v in core.PRESETS.items())}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("inputs", nargs="+", type=Path, help="video file(s) or folders")
    size = p.add_mutually_exclusive_group()
    size.add_argument("-t", "--target", type=_size, metavar="SIZE", help="max output size, e.g. 10M, 800K")
    size.add_argument("-p", "--preset", choices=list(core.PRESETS), help="max size for a chat app")
    p.add_argument("-o", "--output", type=Path, help="output file (single input only)")
    p.add_argument("-d", "--outdir", type=Path, help="write outputs to this folder (default: next to input)")
    p.add_argument("--gif", action="store_true", help="make a GIF instead of an MP4")
    p.add_argument("--codec", choices=list(core.CODECS), default="h264",
                   help="h264 (default, plays everywhere), hevc (~30%% smaller), av1 (smallest, least compatible)")
    p.add_argument("--max-height", type=int, metavar="PX", help="cap height (default 1080 in quality mode)")
    p.add_argument("--fps", type=float, help="cap frame rate (default 30; locks fps in size mode)")
    p.add_argument("--crf", type=int, help="quality-mode CRF (lower = better/bigger; h264 default 26)")
    p.add_argument("--no-audio", action="store_true", help="drop the audio track")
    p.add_argument("--start", type=_time, metavar="T", help="start at T (secs or mm:ss)")
    p.add_argument("--end", type=_time, metavar="T", help="stop at T (secs or mm:ss, of the source)")
    p.add_argument("--speed", type=float, default=1.0, help="playback speed factor, e.g. 2 = twice as fast")
    p.add_argument("--gif-width", type=int, default=800, help="GIF width in px (default 800)")
    p.add_argument("--gif-fps", type=int, default=12, help="GIF frame rate (default 12)")
    p.add_argument("-n", "--dry-run", action="store_true", help="show the plan, encode nothing")
    p.add_argument("-i", "--info", action="store_true", help="print file info and exit")
    p.add_argument("-q", "--quiet", action="store_true", help="no progress output")
    p.add_argument("--json", action="store_true", help="print a JSON summary on stdout")
    p.add_argument("-V", "--version", action="version", version=f"ulepack {__version__}")
    return p


def expand(inputs: list[Path]) -> list[Path]:
    files: list[Path] = []
    for item in inputs:
        item = item.expanduser()
        if item.is_dir():
            files += core.list_videos(item)
        elif item.is_file():
            files.append(item)
        else:
            raise SystemExit(f"not found: {item}")
    return files


def cmd_encode(argv: list[str]) -> int:
    args = encode_parser().parse_args(argv)
    if args.speed <= 0:
        raise SystemExit("--speed must be > 0")
    files = expand(args.inputs)
    if not files:
        raise SystemExit("no videos found")
    if args.output and len(files) > 1:
        raise SystemExit("--output only works with a single input; use --outdir for batches")
    opts = core.Options(
        target=int(core.PRESETS[args.preset] * 1e6) if args.preset else args.target,
        gif=args.gif, codec=args.codec, max_height=args.max_height, fps=args.fps, crf=args.crf,
        no_audio=args.no_audio, start=args.start, end=args.end, speed=args.speed,
        gif_width=args.gif_width, gif_fps=args.gif_fps)
    outdir = args.outdir.expanduser() if args.outdir else None
    tty = sys.stderr.isatty() and not args.quiet
    say = (lambda *a: None) if args.quiet else (lambda msg: print(msg, file=sys.stderr))

    results, failed = [], 0
    for src in files:
        try:
            info = core.probe(src)
        except core.EncodeError as e:
            print(f"error: {e}", file=sys.stderr)
            failed += 1
            continue
        if args.info:
            fps = f"{info.fps:g} fps" if info.fps else "variable fps"
            print(f"{src}: {info.width}x{info.height} {info.vcodec}, {fps}, {info.duration:.1f}s, "
                  f"{core.human(info.size)}, {'audio' if info.has_audio else 'no audio'}")
            continue
        say(f"{src.name}  ({info.width}x{info.height}, {info.duration:.1f}s, {core.human(info.size)})")
        if args.dry_run:
            try:
                say(f"  plan: {core.describe(info, opts)}")
            except core.EncodeError as e:
                print(f"  error: {e}", file=sys.stderr)
                failed += 1
            continue
        out = args.output.expanduser() if args.output else core.default_output(src, opts.gif, outdir)

        def progress(frac: float, label: str) -> None:
            if tty:
                print(f"\r  {label}: {frac * 100:5.1f}%   ", end="", file=sys.stderr, flush=True)

        def log(msg: str) -> None:
            if tty:
                print("\r" + " " * 40 + "\r", end="", file=sys.stderr)
            say(f"  {msg}")

        try:
            res = core.compress(src, out, opts, progress, log, info=info)
        except core.EncodeError as e:
            if tty:
                print(file=sys.stderr)
            print(f"  error: {e}", file=sys.stderr)
            failed += 1
            continue
        if tty:
            print("\r" + " " * 40 + "\r", end="", file=sys.stderr)
        pct = (1 - res.output_bytes / res.input_bytes) * 100 if res.input_bytes else 0
        say(f"  -> {out}  {core.human(res.output_bytes)}  "
            + (f"({pct:.0f}% smaller)" if pct >= 0 else f"(larger than the source by {-pct:.0f}%)"))
        results.append({"input": str(src), "output": str(out.resolve()),
                        "input_bytes": res.input_bytes, "output_bytes": res.output_bytes, "plan": res.plan})
    if args.json:
        print(json.dumps(results, indent=2))
    return 1 if failed else 0


def cmd_ui(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="ulepack ui", description="Local web UI (127.0.0.1 only).")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT}; next free if busy)")
    p.add_argument("--no-open", action="store_true", help="don't open the browser")
    p.add_argument("--url", action="store_true", help="print the UI URL and exit")
    p.add_argument("--folder", type=Path, help="folder to show first (default ~/Videos/Screencasts)")
    p.add_argument("--idle-timeout", type=float, default=5, metavar="MIN",
                   help="stop after MIN minutes without use (never while encoding; 0 = never; default 5)")
    args = p.parse_args(argv)
    if args.idle_timeout < 0:
        raise SystemExit("--idle-timeout must be >= 0")
    from .web.server import serve
    return serve(args.port, not args.no_open, args.url, (args.folder or default_folder()).expanduser(),
                 args.idle_timeout * 60)


def cmd_tui(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="ulepack tui", description="Terminal UI (Textual).")
    p.add_argument("folder", nargs="?", type=Path, help="folder of videos (default ~/Videos/Screencasts)")
    args = p.parse_args(argv)
    folder = (args.folder or default_folder()).expanduser().resolve()
    if not folder.is_dir():
        raise SystemExit(f"not a folder: {folder}")
    try:
        from .tui import ulePackApp
    except ImportError as e:
        raise SystemExit(f"the TUI needs textual ({e}); install with: uv tool install ~/repos/active/ulePack")
    ulePackApp(folder).run()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        core.check_ffmpeg()
        if not argv:
            return cmd_tui([])
        cmd, rest = argv[0], argv[1:]
        if cmd == "presets":
            for k, v in core.PRESETS.items():
                print(f"{k:10} {v:g} MB")
            return 0
        if cmd == "ui" and not Path(cmd).exists():
            return cmd_ui(rest)
        if cmd == "tui" and not Path(cmd).exists():
            return cmd_tui(rest)
        return cmd_encode(argv)
    except core.EncodeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
