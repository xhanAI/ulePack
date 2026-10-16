"""A single-worker job queue over core.compress, shared by the web UI and the TUI.

Encodes are CPU-bound (x264 already uses every core), so jobs run one at a time.
"""
from __future__ import annotations

import itertools
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import core

_ids = itertools.count(1)


@dataclass
class Job:
    src: Path
    opts: core.Options
    outdir: Path | None = None
    id: int = field(default_factory=lambda: next(_ids))
    status: str = "queued"          # queued | running | done | failed | cancelled
    progress: float = 0.0
    label: str = ""
    plan: str = ""
    out: Path | None = None
    in_bytes: int = 0
    out_bytes: int = 0
    duration: float = 0.0
    error: str = ""
    log: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    finished: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict:
        o = self.opts
        if o.target:
            target = next((k for k, v in core.PRESETS.items() if int(v * 1e6) == o.target), None) or core.human(o.target)
        else:
            target = "best quality"
        return {"id": self.id, "src": str(self.src), "name": self.src.name, "status": self.status,
                "progress": round(self.progress, 3), "label": self.label, "plan": self.plan,
                "out": str(self.out) if self.out else None, "out_name": self.out.name if self.out else None,
                "in_bytes": self.in_bytes, "out_bytes": self.out_bytes, "duration": self.duration,
                "error": self.error, "log": self.log[-8:], "target": target, "gif": o.gif,
                "created": self.created, "finished": self.finished}


class Queue:
    def __init__(self, on_change: Callable[[Job], None] | None = None):
        self.jobs: dict[int, Job] = {}
        self.lock = threading.Lock()
        self.version = 0  # bumped on every change; the web UI's poll signature uses it
        self._q: queue.Queue[Job] = queue.Queue()
        self._on_change = on_change
        threading.Thread(target=self._worker, daemon=True, name="ulepack-worker").start()

    def _changed(self, job: Job) -> None:
        with self.lock:
            self.version += 1
        if self._on_change:
            self._on_change(job)

    def add(self, src: Path, opts: core.Options, outdir: Path | None = None) -> Job:
        job = Job(src=src, opts=opts, outdir=outdir)
        try:
            job.in_bytes = src.stat().st_size
        except OSError:
            pass
        with self.lock:
            self.jobs[job.id] = job
        self._q.put(job)
        self._changed(job)
        return job

    def cancel(self, job_id: int) -> None:
        job = self.jobs[job_id]
        job.cancel.set()
        if job.status == "queued":
            job.status, job.finished = "cancelled", time.time()
        self._changed(job)

    def remove(self, job_id: int) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job and job.status in ("done", "failed", "cancelled"):
                del self.jobs[job_id]
        self._changed(job) if job else None

    def clear_finished(self) -> None:
        with self.lock:
            for jid in [j.id for j in self.jobs.values() if j.status in ("done", "failed", "cancelled")]:
                del self.jobs[jid]
            self.version += 1

    def list(self) -> list[Job]:
        with self.lock:
            return sorted(self.jobs.values(), key=lambda j: j.id, reverse=True)

    def busy(self) -> bool:
        return any(j.status in ("queued", "running") for j in self.list())

    def cancel_all(self, wait: float = 10.0) -> None:
        """Cancel every pending job and wait for a running ffmpeg to exit (and remove its
        partial output), so stopping the app never leaves an orphaned encoder behind."""
        for j in self.list():
            if j.status in ("queued", "running"):
                self.cancel(j.id)
        deadline = time.monotonic() + wait
        while any(j.status == "running" for j in self.list()) and time.monotonic() < deadline:
            time.sleep(0.1)

    def _worker(self) -> None:
        while True:
            job = self._q.get()
            if job.cancel.is_set() or job.id not in self.jobs:
                continue
            self._run(job)

    def _run(self, job: Job) -> None:
        job.status, job.label = "running", "probing"
        self._changed(job)
        last = [0.0]

        def progress(frac: float, label: str) -> None:
            job.progress, job.label = frac, label
            if frac - last[0] >= 0.01 or frac >= 1:  # don't bump the version on every ffmpeg tick
                last[0] = frac
                self._changed(job)

        def log(msg: str) -> None:
            job.log.append(msg)
            if msg.startswith("plan: "):
                job.plan = msg[6:]
            self._changed(job)

        try:
            info = core.probe(job.src)
            job.duration = core.clip_duration(info, job.opts)
            job.out = core.default_output(job.src, job.opts.gif, job.outdir)
            res = core.compress(job.src, job.out, job.opts, progress, log, job.cancel, info)
            job.out_bytes, job.status, job.progress, job.label = res.output_bytes, "done", 1.0, "done"
        except core.Cancelled:
            job.status, job.label = "cancelled", "cancelled"
        except Exception as e:  # surface every failure on the job, never kill the worker
            job.status, job.error, job.label = "failed", str(e), "failed"
        job.finished = time.time()
        self._changed(job)
