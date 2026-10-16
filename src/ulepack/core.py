"""The engine: probe a video, plan an encode that fits a size budget, run ffmpeg.

Shared by the CLI, the Textual TUI and the web UI. Stdlib only; needs ffmpeg + ffprobe.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Upload limits in MB (approximate, free tiers; check if a platform changes them).
PRESETS: dict[str, float] = {
    "discord": 10,      # free Discord accounts
    "whatsapp": 16,     # sent as a video (bigger files only go as documents)
    "email": 20,        # leaves room for base64 overhead under a 25 MB cap
    "messenger": 25,
    "signal": 95,
    "viber": 190,
    "slack": 1000,
    "telegram": 2000,
}

# Heights tried (largest first) when a size budget forces downscaling.
LADDER = [2160, 1440, 1080, 900, 720, 540, 480, 360, 240]
# Minimum bits per pixel per frame for screen content to stay readable with x264.
MIN_BPP = 0.035
SAFETY = 0.96  # aim this fraction under the target to absorb container overhead
GENEROUS_BPP = 0.08  # budget this roomy: try a capped quality encode before exact-size two-pass

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".flv", ".wmv", ".ts", ".gif", ".ogv", ".3gp"}

CODECS = {
    # name: (encoder, extra args, two-pass supported)
    "h264": ("libx264", ["-preset", "slow", "-profile:v", "high"], True),
    "hevc": ("libx265", ["-preset", "medium", "-tag:v", "hvc1"], True),
    "av1": ("libsvtav1", ["-preset", "6"], False),
}
CRF_DEFAULT = {"h264": 26, "hevc": 28, "av1": 34}

Progress = Callable[[float, str], None]  # (fraction 0..1, label)
Log = Callable[[str], None]


class EncodeError(Exception):
    pass


class Cancelled(Exception):
    pass


@dataclass
class Info:
    path: Path
    duration: float
    width: int
    height: int
    fps: float | None  # None when the container doesn't know (e.g. GNOME screencasts)
    has_audio: bool
    size: int
    vcodec: str

    def to_dict(self) -> dict:
        return {"path": str(self.path), "duration": self.duration, "width": self.width, "height": self.height,
                "fps": self.fps, "has_audio": self.has_audio, "size": self.size, "vcodec": self.vcodec}


@dataclass
class Options:
    target: int | None = None    # max output bytes; None = quality mode (CRF)
    gif: bool = False
    codec: str = "h264"
    max_height: int | None = None
    fps: float | None = None     # set = locked; None = cap at 30, may drop when the budget is tight
    crf: int | None = None
    no_audio: bool = False
    start: float | None = None
    end: float | None = None
    speed: float = 1.0
    gif_width: int = 800
    gif_fps: int = 12

    @classmethod
    def from_dict(cls, d: dict) -> "Options":
        """Build from untrusted JSON (web UI): only known keys, coerced and range-checked."""
        o = cls()
        num = lambda k, typ, lo, hi: (None if d.get(k) in (None, "") else max(lo, min(hi, typ(d[k]))))
        if d.get("preset"):
            o.target = int(PRESETS[str(d["preset"])] * 1e6)
        elif d.get("target_mb") not in (None, ""):
            o.target = int(float(d["target_mb"]) * 1e6)
            if o.target < 50_000:
                raise ValueError("target must be at least 0.05 MB")
        o.gif = bool(d.get("gif"))
        o.codec = str(d.get("codec") or "h264")
        if o.codec not in CODECS:
            raise ValueError(f"unknown codec {o.codec!r}")
        o.max_height = num("max_height", int, 120, 4320)
        o.fps = num("fps", float, 1, 120)
        o.crf = num("crf", int, 0, 63)
        o.no_audio = bool(d.get("no_audio"))
        o.start = num("start", float, 0, 1e7)
        o.end = num("end", float, 0, 1e7)
        o.speed = num("speed", float, 0.25, 16) or 1.0
        o.gif_width = num("gif_width", int, 120, 1920) or 800
        o.gif_fps = num("gif_fps", int, 2, 30) or 12
        return o


@dataclass
class Result:
    output: Path
    input_bytes: int
    output_bytes: int
    plan: str
    log: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- probing

def check_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise EncodeError(f"{tool} not found; install it (sudo apt install ffmpeg)")


def parse_rate(rate: str | None) -> float | None:
    if not rate or rate in ("0/0", "N/A"):
        return None
    try:
        num, _, den = rate.partition("/")
        val = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return None
    # Variable-frame-rate recorders report time-base rates like 1000/1.
    return val if 1 <= val <= 240 else None


def scan_duration(path: Path) -> float:
    """Decode the whole file to find its length (for containers with no duration)."""
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"],
                          text=True, capture_output=True, timeout=600)
    times = re.findall(r"time=(\d+):(\d+):([\d.]+)", proc.stderr)
    if not times:
        return 0.0
    h, m, s = times[-1]
    return int(h) * 3600 + int(m) * 60 + float(s)


def probe(path: Path) -> Info:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration,size:stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate",
         "-of", "json", str(path)], text=True, capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise EncodeError(f"ffprobe failed on {path.name}: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise EncodeError(f"{path.name}: no video stream")
    fmt = data.get("format", {})
    try:
        duration = float(fmt.get("duration", 0))
    except ValueError:
        duration = 0.0
    if duration <= 0:
        duration = scan_duration(path)
    fps = parse_rate(video.get("avg_frame_rate")) or parse_rate(video.get("r_frame_rate"))
    return Info(path=path, duration=duration, width=int(video.get("width", 0)), height=int(video.get("height", 0)),
                fps=fps, has_audio=any(s.get("codec_type") == "audio" for s in streams),
                size=int(fmt.get("size") or path.stat().st_size), vcodec=video.get("codec_name", "?"))


def thumbnail(path: Path, out: Path, width: int = 320) -> None:
    """Grab one frame (about a second in, or the first frame of short clips) as a JPEG."""
    for ss in ("1", "0"):
        subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-ss", ss, "-i", str(path),
                        "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5", str(out)],
                       capture_output=True, timeout=60)
        if out.exists() and out.stat().st_size:
            return
    raise EncodeError(f"no thumbnail for {path.name}")


# --------------------------------------------------------------------------- helpers

def parse_size(text: str) -> int:
    """'10M', '9.5MB', '800k', '25' (MB) -> bytes (decimal units, as chat apps use)."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([kKmMgG]?)[bB]?\s*", text)
    if not m:
        raise ValueError(f"bad size: {text!r} (try 10M, 800K, 1.5G)")
    num, unit = float(m.group(1)), m.group(2).lower()
    return int(num * {"k": 1e3, "m": 1e6, "g": 1e9, "": 1e6}[unit])


def parse_time(text: str) -> float:
    """'90', '1:30', '00:01:30.5' -> seconds."""
    secs = 0.0
    for p in text.split(":"):
        secs = secs * 60 + float(p)
    return secs


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1000
    return f"{n:.1f} GB"


def even(n: float) -> int:
    return max(2, int(n) // 2 * 2)


def scaled_dims(info: Info, height: int) -> tuple[int, int]:
    """Dimensions after scaling to `height` (never upscales), both even."""
    if height >= info.height:
        return even(info.width), even(info.height)
    return even(info.width * height / info.height), even(height)


def default_output(src: Path, gif: bool = False, outdir: Path | None = None) -> Path:
    ext = ".gif" if gif else ".mp4"
    base = (outdir or src.parent) / f"{src.stem}.shrunk{ext}"
    out, i = base, 2
    while out.exists():
        out = base.with_name(f"{src.stem}.shrunk-{i}{ext}")
        i += 1
    return out


def list_videos(folder: Path) -> list[Path]:
    return sorted((f for f in folder.iterdir()
                   if f.is_file() and f.suffix.lower() in VIDEO_EXTS and ".shrunk" not in f.stem),
                  key=lambda f: f.stat().st_mtime, reverse=True)


def clip_duration(info: Info, o: Options) -> float:
    end = min(o.end, info.duration) if o.end else info.duration
    return max(0.1, (end - (o.start or 0)) / o.speed)


# --------------------------------------------------------------------------- ffmpeg

def run_ffmpeg(cmd: list[str], duration: float, on_frac: Callable[[float], None] | None,
               cancel: threading.Event | None) -> None:
    full = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats", *cmd]
    with tempfile.TemporaryFile("w+") as err:
        proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=err, text=True)
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                if cancel is not None and cancel.is_set():
                    proc.kill()
                    proc.wait()
                    raise Cancelled()
                if on_frac and line.startswith("out_time_us=") and duration > 0:
                    try:
                        on_frac(min(1.0, max(0.0, int(line.split("=", 1)[1]) / 1e6 / duration)))
                    except ValueError:
                        pass
            proc.wait()
        except BaseException:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            raise
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if proc.returncode != 0:
            err.seek(0)
            raise EncodeError(f"ffmpeg failed: {err.read().strip()[-2000:]}")


def input_args(info: Info, o: Options) -> list[str]:
    a: list[str] = []
    if o.start:
        a += ["-ss", str(o.start)]
    if o.end:
        a += ["-to", str(o.end)]
    return a + ["-i", str(info.path)]


def vfilters(info: Info, height: int, fps: float, speed: float) -> str:
    w, h = scaled_dims(info, height)
    f = [f"setpts=PTS/{speed}"] if speed != 1 else []
    f += [f"fps={fps:g}", f"scale={w}:{h}:flags=lanczos", "format=yuv420p"]
    return ",".join(f)


def afilters(speed: float) -> list[str]:
    if speed == 1:
        return []
    chain, s = [], speed  # atempo accepts 0.5..2 per instance, so chain for bigger factors
    while s > 2:
        chain.append("atempo=2")
        s /= 2
    while s < 0.5:
        chain.append("atempo=0.5")
        s /= 0.5
    chain.append(f"atempo={s:g}")
    return ["-af", ",".join(chain)]


def fps_for(info: Info, o: Options) -> float:
    cap = o.fps or 30
    return min(info.fps, cap) if info.fps else cap


def plan_target(info: Info, o: Options, dur: float) -> tuple[int, float, int, int]:
    """Pick (height, fps, video kbps, audio kbps) that fit o.target bytes."""
    budget = o.target or 0
    audio_kbps = 0 if (o.no_audio or not info.has_audio) else (64 if budget < 30e6 else 96)
    total_kbps = budget * 8 * SAFETY / dur / 1000
    video_kbps = int(total_kbps - audio_kbps)
    if video_kbps < 50 and audio_kbps:
        audio_kbps = 32
        video_kbps = int(total_kbps - audio_kbps)
    if video_kbps < 30:
        raise EncodeError(
            f"{human(budget)} is too small for {dur:.0f}s of video (~{int(total_kbps)} kbps). "
            "Trim it, speed it up, or raise the size limit.")
    max_h = min(o.max_height or info.height, info.height)
    heights = [max_h] + [h for h in LADDER if h < max_h]
    fps_options = [fps_for(info, o)]
    if o.fps is None:
        fps_options += [f for f in (24, 15, 10) if f < fps_options[0]]
    for fps in fps_options:
        for h in heights:
            w, hh = scaled_dims(info, h)
            if video_kbps * 1000 / (w * hh * fps) >= MIN_BPP or (h <= 480 and fps <= 15):
                return h, fps, video_kbps, audio_kbps
    return heights[-1], fps_options[-1], video_kbps, audio_kbps


def describe(info: Info, o: Options) -> str:
    """One-line plan, without encoding anything (dry run / UI preview)."""
    dur = clip_duration(info, o)
    if o.gif:
        return f"GIF {min(o.gif_width, even(info.width))}px wide @ {o.gif_fps} fps, {dur:.1f}s" + \
               (f", under {human(o.target)}" if o.target else "")
    audio = not o.no_audio and info.has_audio
    if o.target is None:
        h = min(o.max_height or 1080, info.height)
        w, hh = scaled_dims(info, h)
        crf = o.crf if o.crf is not None else CRF_DEFAULT[o.codec]
        return f"{w}x{hh} @ {fps_for(info, o):g} fps, {o.codec} CRF {crf}{'' if audio else ', no audio'}, {dur:.1f}s"
    h, fps, vk, ak = plan_target(info, o, dur)
    w, hh = scaled_dims(info, h)
    upto = "up to " if vk * 1000 / (w * hh * fps) >= GENEROUS_BPP else ""
    return (f"{w}x{hh} @ {fps:g} fps, video {upto}{vk} kbps{f', audio {ak} kbps' if ak else ', no audio'}"
            f", under {human(o.target)}")


def compress(src: Path, out: Path, o: Options, on_progress: Progress | None = None, on_log: Log | None = None,
             cancel: threading.Event | None = None, info: Info | None = None) -> Result:
    """Encode `src` to `out` per `o`. Raises EncodeError / Cancelled; removes a partial output on failure."""
    check_ffmpeg()
    info = info or probe(src)
    logs: list[str] = []

    def log(msg: str) -> None:
        logs.append(msg)
        if on_log:
            on_log(msg)

    def stage(i: int, n: int, label: str) -> Callable[[float], None]:
        return lambda f: on_progress((i + f) / n, label) if on_progress else None

    plan = describe(info, o)
    log(f"plan: {plan}")
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        (_gif if o.gif else _video)(info, o, out, log, stage, cancel)
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    if on_progress:
        on_progress(1.0, "done")
    return Result(output=out, input_bytes=info.size, output_bytes=out.stat().st_size, plan=plan, log=logs)


def _video(info: Info, o: Options, out: Path, log: Log, stage, cancel) -> None:
    dur = clip_duration(info, o)
    enc, extra, two_pass_ok = CODECS[o.codec]
    audio = not o.no_audio and info.has_audio
    faststart = ["-movflags", "+faststart", "-f", "mp4", str(out)]

    if o.target is None:  # ---- quality mode
        height = min(o.max_height or 1080, info.height)
        crf = o.crf if o.crf is not None else CRF_DEFAULT[o.codec]
        cmd = [*input_args(info, o), "-vf", vfilters(info, height, fps_for(info, o), o.speed),
               "-c:v", enc, *extra, "-crf", str(crf)]
        cmd += ["-c:a", "aac", "-b:a", "128k", *afilters(o.speed)] if audio else ["-an"]
        run_ffmpeg([*cmd, *faststart], dur, stage(0, 1, "encoding"), cancel)
        return

    # ---- size mode
    budget = o.target
    height, fps, vk, ak = plan_target(info, o, dur)
    w, h = scaled_dims(info, height)
    if vk * 1000 / (w * h * fps) >= GENEROUS_BPP:
        # Plenty of room: a bitrate-capped CRF encode keeps easy clips small instead of
        # padding them up to the limit. Only fall back to two-pass if it overshoots.
        crf = o.crf if o.crf is not None else CRF_DEFAULT[o.codec]
        acodec = ["-c:a", "aac", "-b:a", f"{ak}k", *afilters(o.speed)] if ak else ["-an"]
        run_ffmpeg([*input_args(info, o), "-vf", vfilters(info, height, fps, o.speed), "-c:v", enc, *extra,
                    "-crf", str(crf), "-maxrate", f"{vk}k", "-bufsize", f"{vk * 2}k", *acodec, *faststart],
                   dur, stage(0, 1, "encoding"), cancel)
        if out.stat().st_size <= budget:
            return
        log(f"{human(out.stat().st_size)} is over {human(budget)}; switching to an exact-size two-pass encode")
    for attempt in range(1, 4):
        w, h = scaled_dims(info, height)
        tag = f" (try {attempt})" if attempt > 1 else ""
        vf = vfilters(info, height, fps, o.speed)
        rate = ["-b:v", f"{vk}k", "-maxrate", f"{int(vk * 1.5)}k", "-bufsize", f"{vk * 2}k"]
        acodec = ["-c:a", "aac", "-b:a", f"{ak}k", *afilters(o.speed)] if ak else ["-an"]
        with tempfile.TemporaryDirectory(prefix="ulepack-") as tmp:
            logf = os.path.join(tmp, "pass")
            if two_pass_ok:
                if enc == "libx264":
                    first, second = ["-pass", "1", "-passlogfile", logf], ["-pass", "2", "-passlogfile", logf]
                else:
                    first = ["-x265-params", f"pass=1:stats={logf}:log-level=error"]
                    second = ["-x265-params", f"pass=2:stats={logf}:log-level=error"]
                run_ffmpeg([*input_args(info, o), "-vf", vf, "-c:v", enc, *extra, *rate, *first,
                            "-an", "-f", "null", os.devnull], dur, stage(0, 2, f"pass 1/2{tag}"), cancel)
                run_ffmpeg([*input_args(info, o), "-vf", vf, "-c:v", enc, *extra, *rate, *second,
                            *acodec, *faststart], dur, stage(1, 2, f"pass 2/2{tag}"), cancel)
            else:
                run_ffmpeg([*input_args(info, o), "-vf", vf, "-c:v", enc, *extra, *rate, *acodec, *faststart],
                           dur, stage(0, 1, f"encoding{tag}"), cancel)
        size = out.stat().st_size
        if size <= budget:
            return
        log(f"{human(size)} is over {human(budget)}; retrying smaller")
        vk = int(vk * budget / size * 0.92)
        if vk * 1000 / (w * h * fps) < MIN_BPP:
            smaller = [x for x in LADDER if x < h]
            if smaller:
                height = smaller[0]
        log(f"plan: {scaled_dims(info, height)[0]}x{scaled_dims(info, height)[1]} @ {fps:g} fps, video {vk} kbps")
    raise EncodeError(f"could not get under {human(budget)}; trim it or speed it up")


def _gif(info: Info, o: Options, out: Path, log: Log, stage, cancel) -> None:
    dur = clip_duration(info, o)
    widths = [o.gif_width] + [x for x in (640, 480, 360, 240) if x < o.gif_width]
    fps_opts = [o.gif_fps] + [x for x in (8, 6) if x < o.gif_fps]
    tries = [(w, f) for f in fps_opts for w in widths] if o.target else [(widths[0], fps_opts[0])]
    for n, (width, fps) in enumerate(tries, 1):
        width = min(width, even(info.width))
        pre = f"setpts=PTS/{o.speed}," if o.speed != 1 else ""
        vf = (f"{pre}fps={fps},scale={width}:-2:flags=lanczos,split[a][b];"
              "[a]palettegen=stats_mode=diff:max_colors=128[p];"
              "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle")
        run_ffmpeg([*input_args(info, o), "-filter_complex", vf, "-loop", "0", "-f", "gif", str(out)],
                   dur, stage(0, 1, f"GIF {width}px @ {fps} fps" + (f" (try {n})" if n > 1 else "")), cancel)
        if not o.target or out.stat().st_size <= o.target:
            return
        log(f"GIF {width}px @ {fps} fps is {human(out.stat().st_size)}, over {human(o.target)}; going smaller")
    raise EncodeError(f"could not get the GIF under {human(o.target or 0)}; trim it, or send an MP4 instead")
