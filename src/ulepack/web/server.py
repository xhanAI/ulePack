"""Local web UI: a JSON API over core + jobs, plus one static page.

Follows the Local Browser UI Pattern (~/repos/Local Browser UI Pattern.md):
binds to 127.0.0.1, checks the Host header on every request, needs the per-user
token on every /api call (header) and media URL (query), sends no-store, and the
page polls /api/state?since=<sig> (204 when nothing changed).
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

from .. import __version__, core
from ..jobs import Queue

STATIC = Path(__file__).parent / "static"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
DATA = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "ulepack"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ulepack"
UPLOADS = Path.home() / "Videos" / "ulePack"  # dropped files land here, outputs next to them
MAX_JSON = 1_000_000
MAX_UPLOAD = 20 * 1024**3
CHUNK = 1024 * 1024


def ui_token() -> str:
    """Per-user token, kept across launches so the UI URL is stable (bookmarks, widgets)."""
    path = DATA / "ui-token"
    try:
        token = path.read_text().strip()
        if len(token) >= 32:
            return token
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    tmp = path.with_name(f"ui-token.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.replace(tmp, path)
    return token


def build_id() -> str:
    """Fingerprint of the installed code, so a stale UI server can be told apart and replaced."""
    h = hashlib.sha1()
    base = Path(__file__).resolve().parent.parent
    for f in sorted(base.rglob("*")):
        if f.is_file() and "__pycache__" not in f.parts:
            h.update(str(f.relative_to(base)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:12]


BUILD = build_id()


def url_for(port: int, token: str) -> str:
    return f"http://127.0.0.1:{port}/?token={token}"


def running_build(port: int, token: str) -> str | None:
    """Build id of a ulePack UI with our token on the port, or None when none answers."""
    req = Request(f"http://127.0.0.1:{port}/api/state", headers={"X-Ulepack-Token": token})
    try:
        with urlopen(req, timeout=1) as r:
            return str(json.load(r).get("build", "")) if r.status == 200 else None
    except (URLError, OSError, ValueError):
        return None


def stop_running(port: int, token: str) -> None:
    req = Request(f"http://127.0.0.1:{port}/api/shutdown", data=b"{}", method="POST",
                  headers={"X-Ulepack-Token": token, "Content-Type": "application/json"})
    try:
        urlopen(req, timeout=2).read()
    except (URLError, OSError):
        pass


def safe_name(name: str) -> str:
    name = Path(unquote(name)).name.strip().replace("\x00", "")
    name = re.sub(r"[^\w.\- ()\[\]]+", "_", name)[:150]
    return name or "upload.mp4"


def unique(path: Path) -> Path:
    out, i = path, 2
    while out.exists():
        out = path.with_name(f"{path.stem}-{i}{path.suffix}")
        i += 1
    return out


def detach(cmd: list[str], stdin: bytes | None = None) -> None:
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    if stdin is not None and proc.stdin:
        proc.stdin.write(stdin)
        proc.stdin.close()


class Api:
    def __init__(self, folder: Path, idle_timeout: float = 300):
        self.folder = folder
        self.queue = Queue()
        self.thumb_lock = threading.Semaphore(3)  # don't fork 40 ffmpegs when a big folder opens
        self.idle_timeout = idle_timeout  # seconds without use before the server stops itself; 0 = never
        self.last_active = time.time()

    def touch(self) -> None:
        """Note real use: a click, a key, an API action. The page's background polling doesn't count."""
        self.last_active = time.time()

    def idle_deadline(self) -> float | None:
        if not self.idle_timeout:
            return None
        if self.queue.busy():  # encoding counts as use; the countdown starts when the queue empties
            self.touch()
        return self.last_active + self.idle_timeout

    def state(self) -> dict:
        jobs = [j.to_dict() for j in self.queue.list()]
        deadline = self.idle_deadline()
        return {"version": __version__, "build": BUILD, "folder": str(self.folder), "home": str(Path.home()),
                "uploads": str(UPLOADS), "presets": core.PRESETS, "codecs": list(core.CODECS),
                "jobs": jobs, "busy": self.queue.busy(),
                "idle_timeout": self.idle_timeout, "idle_deadline": deadline, "now": time.time(),
                # the deadline moves on every touch; bucket it so polls still get 204 most of the time
                "sig": f"{BUILD}-{self.queue.version}-{int(deadline or 0) // 15}"}

    def browse(self, raw: str) -> dict:
        d = Path(raw or self.folder).expanduser().resolve()
        if not d.is_dir():
            raise ValueError(f"not a folder: {d}")
        dirs, videos = [], []
        try:
            entries = list(d.iterdir())
        except OSError as e:
            raise ValueError(f"cannot read {d}: {e.strerror}") from e
        for e in entries:
            if e.name.startswith("."):
                continue
            try:
                if e.is_dir():
                    dirs.append(e.name)
                elif e.is_file() and e.suffix.lower() in core.VIDEO_EXTS:
                    st = e.stat()
                    videos.append({"name": e.name, "path": str(e), "size": st.st_size, "mtime": st.st_mtime,
                                   "shrunk": ".shrunk" in e.stem})
            except OSError:
                continue
        videos.sort(key=lambda v: (not v["shrunk"], v["mtime"]), reverse=True)  # originals first, newest first
        return {"path": str(d), "parent": str(d.parent) if d.parent != d else None,
                "dirs": sorted(dirs, key=str.lower), "videos": videos}

    def video_path(self, raw: str) -> Path:
        p = Path(raw).expanduser().resolve()
        if not p.is_file():
            raise ValueError(f"not a file: {p}")
        return p

    def probe(self, raw: str, opts: dict | None) -> dict:
        info = core.probe(self.video_path(raw))
        out = info.to_dict()
        if opts is not None:
            try:
                out["plan"] = core.describe(info, core.Options.from_dict(opts))
            except core.EncodeError as e:
                out["plan_error"] = str(e)
        return out

    def thumb(self, raw: str) -> Path:
        p = self.video_path(raw)
        st = p.stat()
        key = hashlib.sha1(f"{p}|{st.st_size}|{st.st_mtime}".encode()).hexdigest()[:20]
        out = CACHE / "thumbs" / f"{key}.jpg"
        if not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            with self.thumb_lock:
                if not out.exists():
                    tmp = out.with_suffix(f".{os.getpid()}.{threading.get_ident()}.jpg")
                    core.thumbnail(p, tmp)
                    os.replace(tmp, out)
        return out

    def add_jobs(self, body: dict) -> dict:
        opts = core.Options.from_dict(body.get("options") or {})
        paths = body.get("paths") or []
        if not isinstance(paths, list) or not paths:
            raise ValueError("no files given")
        outdir = Path(body["outdir"]).expanduser().resolve() if body.get("outdir") else None
        ids = [self.queue.add(self.video_path(str(p)), opts, outdir).id for p in paths]
        return {"ids": ids}

    def job_file(self, job_id: str, which: str) -> Path:
        job = self.queue.jobs[int(job_id)]
        path = job.src if which == "in" else job.out
        if path is None or not path.is_file() or (which == "out" and job.status != "done"):
            raise KeyError("no such file yet")
        return path

    def reveal(self, job_id: int) -> dict:
        job = self.queue.jobs[job_id]
        target = job.out if job.out and job.out.exists() else job.src
        if shutil.which("nautilus"):
            detach(["nautilus", "--select", str(target)])
        else:
            detach(["xdg-open", str(target.parent)])
        return {"ok": True}

    def copy_file(self, job_id: int) -> dict:
        """Put the output on the clipboard as a file (paste into Files, Slack, Discord, ...)."""
        job = self.queue.jobs[job_id]
        if not job.out or not job.out.exists():
            raise ValueError("output not ready")
        if not shutil.which("xclip"):
            raise ValueError("xclip is not installed (sudo apt install xclip)")
        uri = "file://" + quote(str(job.out))
        detach(["xclip", "-selection", "clipboard", "-t", "text/uri-list", "-i"], (uri + "\r\n").encode())
        return {"ok": True}


def make_server(folder: Path, port: int, token: str, idle_timeout: float = 300) -> ThreadingHTTPServer:
    api = Api(folder, idle_timeout)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ulepack"

        def log_message(self, *args):  # keep the terminal quiet
            pass

        def _headers(self, code: int, ctype: str, length: int, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
                             "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self._headers(code, ctype, len(body))
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json")

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            return host in ("127.0.0.1", "localhost", "::1")

        def _authed(self, query: dict | None = None) -> bool:
            given = self.headers.get("X-Ulepack-Token", "") or (query or {}).get("token", [""])[0]
            return secrets.compare_digest(given, token)

        def _file(self, path: Path, download: bool = False) -> None:
            """Serve a file with Range support (the <video> player needs it to seek)."""
            size = path.stat().st_size
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            start, end, code = 0, size - 1, 200
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
            if m and size:
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2) or 0))
                if start > end or start >= size:
                    return self._headers(416, "text/plain", 0, {"Content-Range": f"bytes */{size}"})
                code = 206
            extra = {"Accept-Ranges": "bytes"}
            if code == 206:
                extra["Content-Range"] = f"bytes {start}-{end}/{size}"
            if download:
                extra["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(path.name)}"
            self._headers(code, ctype, end - start + 1, extra)
            if self.command == "HEAD":
                return
            with path.open("rb") as f:
                f.seek(start)
                left = end - start + 1
                try:
                    while left > 0:
                        chunk = f.read(min(CHUNK, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._host_ok():
                return self._json({"error": "bad host"}, 403)
            url = urlparse(self.path)
            q = parse_qs(url.query)
            p = url.path
            if p == "/":
                if not self._authed(q):
                    return self._send(403, b"Open the URL printed by `ulepack ui` (it carries a token).",
                                      "text/plain; charset=utf-8")
                page = (STATIC / "index.html").read_text().replace("__TOKEN__", token)
                return self._send(200, page.encode(), TYPES[".html"])
            if p == "/favicon.ico":
                return self._file(STATIC / "icon.svg")
            if not p.startswith(("/api/", "/media/")):
                f = STATIC / p.lstrip("/")
                if f.suffix in TYPES and f.parent == STATIC and f.is_file():
                    return self._send(200, f.read_bytes(), TYPES[f.suffix])
                return self._send(404, b"not found", "text/plain")
            if not self._authed(q):
                return self._json({"error": "bad token"}, 403)
            if p != "/api/state":
                api.touch()
            try:
                if p == "/api/state":
                    st = api.state()
                    if q.get("since", [""])[0] == st["sig"]:
                        return self._headers(204, "application/json", 0)
                    return self._json(st)
                if p == "/api/browse":
                    return self._json(api.browse(q.get("path", [""])[0]))
                if p == "/api/probe":
                    opts = json.loads(q["opts"][0]) if "opts" in q else None
                    return self._json(api.probe(q.get("path", [""])[0], opts))
                if p == "/media/thumb":
                    return self._file(api.thumb(q.get("path", [""])[0]))
                m = re.fullmatch(r"/media/(\d+)/(in|out)", p)
                if m:
                    return self._file(api.job_file(m.group(1), m.group(2)), download="download" in q)
                return self._json({"error": "unknown endpoint"}, 404)
            except KeyError as e:
                return self._json({"error": f"not found: {e}"}, 404)
            except (ValueError, core.EncodeError) as e:
                return self._json({"error": str(e)}, 400)
            except (BrokenPipeError, ConnectionResetError):
                return None
            except Exception as e:  # surface, don't drop the connection
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        def _upload(self) -> None:
            length = int(self.headers.get("Content-Length") or -1)
            if not 0 < length <= MAX_UPLOAD:
                return self._json({"error": "bad Content-Length"}, 400)
            name = safe_name(self.headers.get("X-Filename", ""))
            if Path(name).suffix.lower() not in core.VIDEO_EXTS:
                return self._json({"error": f"not a video file: {name}"}, 400)
            UPLOADS.mkdir(parents=True, exist_ok=True)
            dest = unique(UPLOADS / name)
            tmp = dest.with_name(dest.name + ".part")
            try:
                with tmp.open("wb") as f:
                    left = length
                    while left > 0:
                        chunk = self.rfile.read(min(CHUNK, left))
                        if not chunk:
                            raise ValueError("upload interrupted")
                        f.write(chunk)
                        left -= len(chunk)
                os.replace(tmp, dest)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
            self._json({"path": str(dest)})

        def do_POST(self):
            if not self._host_ok() or not self._authed():
                return self._json({"error": "forbidden"}, 403)
            path = urlparse(self.path).path
            api.touch()
            try:
                if path == "/api/upload":
                    return self._upload()
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    return self._json({"error": "json only"}, 415)
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 <= length <= MAX_JSON:
                    return self._json({"error": "bad Content-Length"}, 400)
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
                if path == "/api/jobs":
                    return self._json(api.add_jobs(body))
                if path == "/api/plan":
                    return self._json(api.probe(str(body["path"]), body.get("options") or {}))
                if path == "/api/cancel":
                    api.queue.cancel(int(body["id"]))
                    return self._json({"ok": True})
                if path == "/api/remove":
                    api.queue.remove(int(body["id"]))
                    return self._json({"ok": True})
                if path == "/api/clear":
                    api.queue.clear_finished()
                    return self._json({"ok": True})
                if path == "/api/reveal":
                    return self._json(api.reveal(int(body["id"])))
                if path == "/api/copyfile":
                    return self._json(api.copy_file(int(body["id"])))
                if path == "/api/ping":  # the page saw the user click/type/scroll
                    return self._json({"idle_deadline": api.idle_deadline()})
                if path == "/api/shutdown":
                    self._json({"ok": True})
                    stop(self.server, "stopped from the browser")
                    return None
                return self._json({"error": "unknown endpoint"}, 404)
            except KeyError as e:
                return self._json({"error": f"missing or unknown: {e}"}, 400)
            except (ValueError, TypeError, core.EncodeError) as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:  # surface, don't kill the server
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    httpd.api = api  # type: ignore[attr-defined]
    httpd.stop_reason = ""  # type: ignore[attr-defined]
    return httpd


def stop(httpd: ThreadingHTTPServer, reason: str) -> None:
    """Stop serving (from any thread): cancel encodes so no ffmpeg is orphaned, then free the port."""
    if getattr(httpd, "stop_reason", ""):
        return
    httpd.stop_reason = reason  # type: ignore[attr-defined]

    def run() -> None:
        httpd.api.queue.cancel_all()  # type: ignore[attr-defined]
        httpd.shutdown()

    threading.Thread(target=run, daemon=True).start()


def idle_watchdog(httpd: ThreadingHTTPServer) -> None:
    api: Api = httpd.api  # type: ignore[attr-defined]
    while not httpd.stop_reason:  # type: ignore[attr-defined]
        time.sleep(5)
        deadline = api.idle_deadline()
        if deadline is not None and time.time() >= deadline:
            stop(httpd, f"stopped after {api.idle_timeout / 60:g} min without use")
            return


def serve(port: int, open_browser: bool, url_only: bool, folder: Path, idle_timeout: float = 300) -> int:
    token = ui_token()
    if url_only:
        print(url_for(port, token))
        return 0
    running = running_build(port, token)
    restarted = False
    if running is not None and running != BUILD:
        print("Restarting the ulePack UI: the running one is older code.")
        stop_running(port, token)
        running, restarted = None, True
    if running is not None:
        url = url_for(port, token)
        print(f"ulePack UI already running: {url}")
        if open_browser:
            webbrowser.open(url)
        return 0
    httpd = None
    for _ in range(30 if restarted else 0):  # a just-stopped server needs a moment to free the port
        try:
            httpd = make_server(folder, port, token, idle_timeout)
            break
        except OSError:
            time.sleep(0.1)
    for p in range(port, port + 30) if httpd is None else ():
        try:
            httpd = make_server(folder, p, token, idle_timeout)
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit(f"no free port in {port}-{port + 29}")
    url = url_for(httpd.server_address[1], token)
    idle = f"stops after {idle_timeout / 60:g} min without use, " if idle_timeout else ""
    print(f"ulePack UI: {url}\n({idle}Stop button in the page, or Ctrl+C)", flush=True)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    if idle_timeout:
        threading.Thread(target=idle_watchdog, args=(httpd,), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.stop_reason = "stopped with Ctrl+C"  # type: ignore[attr-defined]
        httpd.api.queue.cancel_all()  # type: ignore[attr-defined]
    finally:
        httpd.server_close()
    print(f"ulePack UI {httpd.stop_reason or 'stopped'}; port {httpd.server_address[1]} is free.")  # type: ignore[attr-defined]
    return 0
