"""Textual TUI: pick videos in a folder, pick a size, shrink. Same job queue as the web UI."""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.theme import Theme
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Select, Static, Switch

from . import core
from .jobs import Job, Queue

# git-odos tokens, same as tidyUp
INFO, OK, WARN, DANGER, INK_3 = "#5B82FF", "#3DD68C", "#F2B84B", "#FF5A6E", "#6F7D92"
GIT_ODOS = Theme(
    name="git-odos", dark=True, primary=INFO, secondary="#A3B0C2", accent=INFO,
    foreground="#E4EAF2", background="#0D1420", surface="#141D2C", panel="#1B2638",
    success=OK, warning=WARN, error=DANGER, variables={"border": "#27344A", "text-muted": INK_3})
GIT_ODOS_LIGHT = Theme(
    name="git-odos-light", dark=False, primary=INFO, secondary="#4A5669", accent=INFO,
    foreground="#101B2D", background="#E8ECF1", surface="#F6F8FB", panel="#FFFFFF",
    success=OK, warning=WARN, error=DANGER, variables={"border": "#C6CFDA", "text-muted": "#7C8799"})

STATUS_COLOR = {"queued": INK_3, "running": INFO, "done": OK, "failed": DANGER, "cancelled": WARN}


def ago(ts: float) -> str:
    s = time.time() - ts
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


class ulePackApp(App):
    TITLE = "ulePack"
    CSS = """
    #body { height: 1fr; }
    #files { width: 1fr; border: round $border; }
    #side { width: 44; padding: 0 1; border: round $border; }
    #side Label { margin-top: 1; color: $text-muted; }
    #side Select, #side Input { width: 100%; }
    .row { height: 3; }
    .row Label { margin-top: 1; width: 1fr; color: $foreground; }
    #plan { margin-top: 1; color: $text-muted; height: auto; }
    #go { width: 100%; margin-top: 1; }
    #jobs { height: 12; border: round $border; }
    """
    BINDINGS = [
        ("space", "toggle", "Select"),
        ("a", "select_all", "All"),
        ("n", "select_none", "None"),
        ("s", "shrink", "Shrink"),
        ("g", "toggle_gif", "GIF"),
        ("r", "reload", "Reload"),
        ("o", "open_folder", "Open folder"),
        ("t", "toggle_theme", "Theme"),
        ("q", "quit_safe", "Quit"),
    ]

    def __init__(self, folder: Path):
        super().__init__()
        self.folder = folder
        self.selected: set[str] = set()
        self.videos: list[Path] = []
        self.queue = Queue(on_change=self._job_changed)
        self._quit_armed = 0.0

    # ---- layout
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            yield DataTable(id="files", cursor_type="row", zebra_stripes=True)
            with Vertical(id="side"):
                yield Label("Make it fit")
                yield Select([("Best quality (no limit)", "")] +
                             [(f"{k.capitalize()}  {v:g} MB", k) for k, v in core.PRESETS.items()],
                             value="discord", allow_blank=False, id="preset")
                yield Label("Custom limit (MB, overrides)")
                yield Input(placeholder="e.g. 8", type="number", id="custom")
                with Horizontal(classes="row"):
                    yield Label("GIF instead of MP4")
                    yield Switch(id="gif")
                with Horizontal(classes="row"):
                    yield Label("Drop audio")
                    yield Switch(id="noaudio")
                yield Static(id="plan")
                yield Button("Shrink", id="go", variant="primary")
        yield DataTable(id="jobs", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(GIT_ODOS)
        self.register_theme(GIT_ODOS_LIGHT)
        self.theme = "git-odos"
        files = self.query_one("#files", DataTable)
        files.add_column(" ", key="sel", width=2)
        files.add_columns("Name", "Size", "Modified")
        jobs = self.query_one("#jobs", DataTable)
        jobs.add_column("File", key="file")
        jobs.add_column("Target", key="target")
        jobs.add_column("Status", key="status")
        jobs.add_column("Result", key="result")
        jobs.border_title = "Jobs"
        self.action_reload()
        files.focus()

    # ---- files
    def action_reload(self) -> None:
        files = self.query_one("#files", DataTable)
        files.border_title = str(self.folder).replace(str(Path.home()), "~")
        files.clear()
        self.videos = core.list_videos(self.folder)
        for v in self.videos:
            st = v.stat()
            files.add_row(self._mark(str(v)), v.name, core.human(st.st_size), ago(st.st_mtime), key=str(v))
        if not self.videos:
            self.notify(f"No videos in {self.folder}", severity="warning")
        self._update_plan()

    def _mark(self, key: str) -> str:
        return f"[{INFO}]●[/]" if key in self.selected else f"[{INK_3}]○[/]"

    def _set_mark(self, key: str) -> None:
        self.query_one("#files", DataTable).update_cell(key, "sel", self._mark(key))

    def _current(self) -> str | None:
        files = self.query_one("#files", DataTable)
        if not self.videos or files.cursor_row is None:
            return None
        return str(self.videos[files.cursor_row])

    def action_toggle(self) -> None:
        key = self._current()
        if key is None:
            return
        self.selected.symmetric_difference_update({key})
        self._set_mark(key)
        files = self.query_one("#files", DataTable)
        files.move_cursor(row=min(files.cursor_row + 1, len(self.videos) - 1))
        self._update_plan()

    def action_select_all(self) -> None:
        self.selected = {str(v) for v in self.videos}
        for v in self.videos:
            self._set_mark(str(v))
        self._update_plan()

    def action_select_none(self) -> None:
        old, self.selected = self.selected, set()
        for k in old:
            self._set_mark(k)
        self._update_plan()

    @on(DataTable.RowHighlighted, "#files")
    def _highlighted(self) -> None:
        self._update_plan()

    @on(DataTable.RowSelected, "#files")
    def _clicked(self) -> None:  # Enter or a click on a row
        self.action_toggle()

    # ---- options
    def options(self) -> core.Options:
        preset = self.query_one("#preset", Select).value
        custom = self.query_one("#custom", Input).value.strip()
        o = core.Options(gif=self.query_one("#gif", Switch).value, no_audio=self.query_one("#noaudio", Switch).value)
        if custom:
            o.target = int(float(custom) * 1e6)
        elif preset:
            o.target = int(core.PRESETS[str(preset)] * 1e6)
        return o

    @on(Select.Changed)
    @on(Input.Changed)
    @on(Switch.Changed)
    def _opts_changed(self) -> None:
        self._update_plan()

    def action_toggle_gif(self) -> None:
        sw = self.query_one("#gif", Switch)
        sw.value = not sw.value

    def _update_plan(self) -> None:
        targets = sorted(self.selected) or ([self._current()] if self._current() else [])
        plan = self.query_one("#plan", Static)
        go = self.query_one("#go", Button)
        n = len(self.selected)
        go.label = f"Shrink {n} selected" if n else "Shrink highlighted"
        go.disabled = not targets
        if not targets:
            plan.update("No video selected.")
            return
        self._plan_worker(targets[0])

    def _plan_worker(self, path: str) -> None:
        try:
            opts = self.options()
        except ValueError:
            self.query_one("#plan", Static).update(f"[{DANGER}]bad custom size[/]")
            return

        def work() -> None:
            try:
                info = core.probe(Path(path))
                text = (f"{Path(path).name}\n{info.width}x{info.height}, {info.duration:.1f}s, {core.human(info.size)}\n"
                        f"→ {core.describe(info, opts)}")
            except core.EncodeError as e:
                text = f"[{DANGER}]{e}[/]"
            self.call_from_thread(self.query_one("#plan", Static).update, text)

        threading.Thread(target=work, daemon=True).start()

    # ---- jobs
    @on(Button.Pressed, "#go")
    def action_shrink(self) -> None:
        targets = sorted(self.selected) or ([self._current()] if self._current() else [])
        if not targets:
            return
        try:
            opts = self.options()
        except ValueError:
            self.notify("Custom size must be a number of MB", severity="error")
            return
        for t in targets:
            self.queue.add(Path(t), opts)
        self.notify(f"Queued {len(targets)} job(s)")
        self.action_select_none()

    def _job_changed(self, job: Job) -> None:
        if threading.current_thread() is threading.main_thread():
            self._render_job(job)
        else:
            self.call_from_thread(self._render_job, job)

    def _render_job(self, job: Job) -> None:
        jobs = self.query_one("#jobs", DataTable)
        d = job.to_dict()
        color = STATUS_COLOR.get(job.status, INK_3)
        if job.status == "running":
            bar = int(job.progress * 20)
            status = f"[{color}]{'█' * bar}{'░' * (20 - bar)}[/] {job.progress * 100:4.0f}% {job.label}"
        else:
            status = f"[{color}]{job.status}[/]"
        if job.status == "done":
            pct = (1 - job.out_bytes / job.in_bytes) * 100 if job.in_bytes else 0
            result = f"{core.human(job.in_bytes)} → [b]{core.human(job.out_bytes)}[/b] ({pct:.0f}% smaller)  {job.out}"
        else:
            result = f"[{DANGER}]{job.error}[/]" if job.error else (job.plan or "")
        key = str(job.id)
        target = ("GIF · " if job.opts.gif else "") + d["target"]
        if key in jobs.rows:
            jobs.update_cell(key, "status", status)
            jobs.update_cell(key, "result", result)
        else:
            jobs.add_row(job.src.name, target, status, result, key=key)
        if job.status == "done":
            self.notify(f"{job.src.name}: {core.human(job.out_bytes)}", title="Done")
            if job.out and job.out.parent == self.folder:
                pass  # outputs are hidden from the list (".shrunk"), nothing to reload
        elif job.status == "failed":
            self.notify(job.error, title=job.src.name, severity="error")

    # ---- misc
    def action_open_folder(self) -> None:
        subprocess.Popen(["xdg-open", str(self.folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)

    def action_toggle_theme(self) -> None:
        self.theme = "git-odos-light" if self.theme == "git-odos" else "git-odos"

    def action_quit_safe(self) -> None:
        if self.queue.busy() and time.time() - self._quit_armed > 3:
            self._quit_armed = time.time()
            self.notify("Encodes are still running. Press q again to quit and stop them.", severity="warning")
            return
        self.queue.cancel_all()
        self.exit()
