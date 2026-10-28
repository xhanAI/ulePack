"use strict";
const TOKEN = document.querySelector('meta[name="ulepack-token"]').content;
const LABELS = { discord: "Discord", whatsapp: "WhatsApp", email: "Email", messenger: "Messenger",
                 signal: "Signal", viber: "Viber", slack: "Slack", telegram: "Telegram" };
const SETTINGS_KEY = "ulepack-settings";

const S = { state: null, sig: "", folder: null, browse: null, sel: new Set(),
            opt: { preset: "discord", target_mb: "", gif: false, codec: "h264", max_height: "", fps: "",
                   gif_width: "800", gif_fps: "12", speed: "1", start: "", end: "", no_audio: false,
                   cuts: "", fade_in: "", fade_out: "", rotate: "", flip: "", crop: "" } };
// Edits belong to one video, so they're never remembered: a saved cut would silently hit the next file.
const EDITS = ["cuts", "fade_in", "fade_out", "rotate", "flip", "crop"];
try {
  const saved = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
  for (const k of EDITS) delete saved[k];
  Object.assign(S.opt, saved);
} catch (e) { /* none saved */ }
const save = () => {
  const keep = { ...S.opt }; for (const k of EDITS) delete keep[k];
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(keep)); } catch (e) { /* blocked */ }
};
const $ = (id) => document.getElementById(id);

// ---- helpers (DOM only: file names are untrusted, never innerHTML) ----
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "style") e.style.cssText = v;
    else if (v === true) e.setAttribute(k, "");
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) e.append(kid.nodeType ? kid : document.createTextNode(kid));
  return e;
}
function human(n) {  // decimal units, like the chat apps' limits
  for (const u of ["B", "KB", "MB", "GB"]) { if (Math.abs(n) < 1000 || u === "GB") return u === "B" ? `${Math.round(n)} B` : `${n.toFixed(1)} ${u}`; n /= 1000; }
}
function ago(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now"; if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}
function toast(msg, err) {
  const t = h("div", { class: "toast" + (err ? " err" : "") }, msg);
  document.body.append(t); setTimeout(() => t.remove(), err ? 7000 : 3500);
}
async function api(path, body) {
  const r = await fetch(path, { method: body ? "POST" : "GET", cache: "no-store",
    headers: { "X-Ulepack-Token": TOKEN, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined });
  if (r.status === 204) return null;
  const text = await r.text();
  let j = null; try { j = JSON.parse(text); } catch (e) { /* not JSON */ }
  if (!r.ok) throw new Error((j && j.error) || text || r.statusText);
  return j;
}
const media = (p, extra = "") => `${p}${p.includes("?") ? "&" : "?"}token=${encodeURIComponent(TOKEN)}${extra}`;
const thumbUrl = (path) => media(`/media/thumb?path=${encodeURIComponent(path)}`);

function options() {
  const o = { ...S.opt };
  if (o.target_mb) o.preset = ""; else o.target_mb = "";
  return o;
}

// ---- live state (poll + signature, 204 when unchanged) ----
let pollTimer;
async function poll() {
  clearTimeout(pollTimer);
  try {
    const st = await api(`/api/state?since=${S.sig}`);
    if (S.down) return location.reload();  // server is back (restarted): start fresh
    if (st) {
      const first = !S.state; S.state = st; S.sig = st.sig;
      S.deadline = st.idle_deadline; S.skew = st.now - Date.now() / 1000;
      if (first) init(); renderJobs();
    }
    $("live").className = "live on";
  } catch (e) {
    $("live").className = "live off";
    if (S.stopping || (S.deadline && idleLeft() <= 10)) showStopped();
  }
  const busy = S.state?.busy;
  pollTimer = setTimeout(poll, S.down ? 5000 : busy ? 700 : 1500);
}

// ---- stop button + idle auto-stop ----
const idleLeft = () => S.deadline - (Date.now() / 1000 + (S.skew || 0));
function showStopped() {
  if (S.down) return;
  S.down = true;
  const mins = (S.state?.idle_timeout || 0) / 60;
  $("stopreason").textContent = S.stopping ? "You stopped it."
    : `It stopped itself after ${mins} min without use.`;
  $("idlebar").hidden = true; $("stopped").hidden = false; $("live").className = "live off";
  document.title = "ulePack (stopped)";
}
function idleTick() {
  const bar = $("idlebar");
  if (S.down || !S.deadline || S.state?.busy) { bar.hidden = true; return; }
  const left = Math.max(0, Math.round(idleLeft()));
  bar.hidden = left > 60;
  if (!bar.hidden) $("idletext").textContent =
    `No activity. ulePack stops in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")} to free its port.`;
  if (left <= 0) poll();
}
setInterval(idleTick, 1000);
let lastPing = 0;
async function ping(force) {
  if (S.down || !S.state || (!force && Date.now() - lastPing < 20000)) return;
  lastPing = Date.now();
  try { const r = await api("/api/ping", {}); S.deadline = r.idle_deadline; idleTick(); } catch (e) { /* poll handles it */ }
}
for (const ev of ["pointerdown", "pointermove", "keydown", "wheel", "touchstart", "dragenter"])
  window.addEventListener(ev, () => ping(false), { passive: true });
$("keep").onclick = () => ping(true);
$("stop").onclick = async () => {
  const running = (S.state?.jobs || []).filter((j) => j.status === "running" || j.status === "queued").length;
  if (running && !confirm(`${running} job(s) still running or queued. Stop anyway? They'll be cancelled.`)) return;
  S.stopping = true;
  try { await api("/api/shutdown", {}); } catch (e) { /* already gone */ }
  showStopped(); clearTimeout(pollTimer); pollTimer = setTimeout(poll, 5000);
  window.close();  // only works for script-opened tabs; otherwise the stopped screen stays
};
$("closetab").onclick = () => { window.close(); toast("The browser won't let the page close this tab. Close it yourself (Ctrl+W)."); };
document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });

function init() {
  const st = S.state;
  $("version").textContent = `v${st.version}`;
  $("uploads").textContent = st.uploads.replace(st.home, "~");
  const places = [["Screencasts", `${st.home}/Videos/Screencasts`], ["Videos", `${st.home}/Videos`],
                  ["Downloads", `${st.home}/Downloads`], ["Desktop", `${st.home}/Desktop`], ["Dropped files", st.uploads]];
  $("places").replaceChildren(...places.map(([n, p]) => h("button", { class: "chip", onclick: () => openFolder(p) }, n)));
  let start = st.folder;
  try { start = sessionStorage.getItem("ulepack-folder") || start; } catch (e) { /* blocked */ }
  openFolder(start).catch(() => openFolder(st.folder));
  renderTargets(); renderOptions();
}

// ---- folder browser ----
async function openFolder(path) {
  try {
    const b = await api(`/api/browse?path=${encodeURIComponent(path)}`);
    if (S.browse?.path !== b.path) S.sel.clear();
    S.browse = b; $("path").value = b.path;
    try { sessionStorage.setItem("ulepack-folder", b.path); } catch (e) { /* blocked */ }
    renderBrowse();
  } catch (e) { toast(e.message, true); throw e; }
}
function renderBrowse() {
  const b = S.browse;
  $("dirs").replaceChildren(...b.dirs.slice(0, 60).map((d) =>
    h("button", { class: "chip", onclick: () => openFolder(`${b.path}/${d}`) }, d)));
  const grid = $("grid");
  if (!b.videos.length) { grid.replaceChildren(h("div", { class: "empty" }, "No videos in this folder.")); }
  else grid.replaceChildren(...b.videos.map((v) => {
    const on = S.sel.has(v.path);
    return h("button", { class: `tile${on ? " on" : ""}${v.shrunk ? " shrunk" : ""}`, title: v.name,
                         onclick: () => { on ? S.sel.delete(v.path) : S.sel.add(v.path); renderBrowse(); },
                         ondblclick: () => previewSource(v) },
      h("img", { class: "th", loading: "lazy", alt: "", src: thumbUrl(v.path) }),
      h("span", { class: "tick" }),
      v.shrunk ? h("span", { class: "tag" }, "shrunk") : null,
      h("div", { class: "meta" }, h("div", { class: "nm" }, v.name),
        h("div", { class: "sub" }, h("span", {}, human(v.size)), h("span", {}, ago(v.mtime)))));
  }));
  updateRun();
}
$("up").onclick = () => S.browse?.parent && openFolder(S.browse.parent);
$("go").onclick = () => openFolder($("path").value.trim());
$("path").addEventListener("keydown", (e) => { if (e.key === "Enter") openFolder($("path").value.trim()); });
$("selall").onclick = () => { for (const v of S.browse?.videos || []) if (!v.shrunk) S.sel.add(v.path); renderBrowse(); };
$("selnone").onclick = () => { S.sel.clear(); renderBrowse(); };

// ---- options ----
function renderTargets() {
  const presets = S.state.presets;
  const chip = (key, label, mb) => h("button", {
    class: "chip" + (S.opt.preset === key && !S.opt.target_mb ? " on" : ""),
    onclick: () => { S.opt.preset = key; S.opt.target_mb = ""; $("custom").value = ""; changed(); } },
    label, mb ? h("span", { class: "mb" }, mb >= 1000 ? `${mb / 1000} GB` : `${mb} MB`) : null);
  $("targets").replaceChildren(chip("", "Best quality"),
    ...Object.entries(presets).map(([k, mb]) => chip(k, LABELS[k] || k, mb)));
  $("custom").value = S.opt.target_mb;
}
function renderOptions() {
  for (const id of ["codec", "max_height", "fps", "gif_width", "gif_fps", "speed", "start", "end", ...EDITS]) $(id).value = S.opt[id];
  const n = EDITS.filter((k) => S.opt[k]).length;
  $("edit_sum").textContent = n ? `Edit (${n} on)` : "Edit";
  $("no_audio").checked = !!S.opt.no_audio;
  document.body.classList.toggle("gif", !!S.opt.gif);
  for (const b of $("format").children) b.classList.toggle("on", (b.dataset.v === "gif") === !!S.opt.gif);
}
function changed() { save(); renderTargets(); renderOptions(); updateRun(); }
$("custom").addEventListener("input", (e) => { S.opt.target_mb = e.target.value; save(); renderTargets(); updateRun(); });
for (const b of $("format").children) b.onclick = () => { S.opt.gif = b.dataset.v === "gif"; changed(); };
for (const id of ["codec", "max_height", "fps", "gif_width", "gif_fps", "speed", "fade_in", "fade_out", "rotate", "flip"]) $(id).onchange = (e) => { S.opt[id] = e.target.value; changed(); };
for (const id of ["start", "end", "cuts", "crop"]) $(id).addEventListener("change", (e) => { S.opt[id] = e.target.value.trim(); changed(); });
$("no_audio").onchange = (e) => { S.opt.no_audio = e.target.checked; changed(); };

function toSeconds(t) {
  if (!t) return "";
  const parts = t.split(":").map(Number);
  if (parts.some((x) => Number.isNaN(x))) throw new Error(`bad time "${t}" (use 1:30 or 90)`);
  return parts.reduce((a, x) => a * 60 + x, 0);
}
function payload() {
  const o = options();
  o.start = toSeconds(o.start); o.end = toSeconds(o.end);
  return o;
}

let planTimer, planSeq = 0;
function updateRun() {
  const n = S.sel.size, btn = $("run");
  btn.disabled = !n;
  const what = S.opt.gif ? "GIF" : "video";
  const to = S.opt.target_mb ? ` to under ${S.opt.target_mb} MB` : S.opt.preset ? ` for ${LABELS[S.opt.preset] || S.opt.preset}` : "";
  btn.textContent = n ? `Shrink ${n} ${what}${n > 1 ? "s" : ""}${to}` : "Select a video";
  $("selcount").textContent = n ? `${n} selected` : "";
  clearTimeout(planTimer);
  planTimer = setTimeout(showPlan, 250);
}
async function showPlan() {
  const el = $("plan"), first = [...S.sel][0], seq = ++planSeq;
  if (!first) { el.textContent = ""; el.className = "plan"; return; }
  try {
    const r = await api("/api/plan", { path: first, options: payload() });
    if (seq !== planSeq) return;
    const src = `${r.width}×${r.height}, ${r.duration.toFixed(1)}s, ${human(r.size)}`;
    el.className = "plan" + (r.plan_error ? " err" : "");
    el.replaceChildren(h("div", { class: "muted" }, `Source: ${src}`),
                       h("div", {}, r.plan_error ? r.plan_error : `Plan: ${r.plan}`),
                       h("div", { class: "muted" }, S.sel.size > 1 ? "(plan for the first file; each gets its own)" : ""));
  } catch (e) { if (seq === planSeq) { el.className = "plan err"; el.textContent = e.message; } }
}

$("run").onclick = async () => {
  try {
    const r = await api("/api/jobs", { paths: [...S.sel], options: payload() });
    toast(`Queued ${r.ids.length} job${r.ids.length > 1 ? "s" : ""}`);
    S.sel.clear(); renderBrowse(); poll();
  } catch (e) { toast(e.message, true); }
};

// ---- drop / pick files ----
async function uploadAll(files) {
  const vids = [...files].filter((f) => /\.(mp4|mkv|webm|mov|avi|m4v|flv|wmv|ts|gif|ogv|3gp)$/i.test(f.name));
  if (!vids.length) return toast("No video files in that drop", true);
  const paths = [];
  for (const [i, f] of vids.entries()) {
    $("drop").querySelector("b").textContent = `Copying ${i + 1}/${vids.length}: ${f.name}…`;
    try {
      const r = await fetch("/api/upload", { method: "POST", body: f,
        headers: { "X-Ulepack-Token": TOKEN, "X-Filename": encodeURIComponent(f.name), "Content-Type": "application/octet-stream" } });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || r.statusText);
      paths.push(j.path);
    } catch (e) { toast(`${f.name}: ${e.message}`, true); }
  }
  $("drop").querySelector("b").textContent = "Drop videos here";
  if (!paths.length) return;
  await openFolder(S.state.uploads);
  for (const p of paths) S.sel.add(p);
  renderBrowse();
  toast(`Added ${paths.length} file${paths.length > 1 ? "s" : ""}. Pick a size and hit Shrink.`);
}
$("drop").onclick = () => $("picker").click();
$("picker").onchange = (e) => { uploadAll(e.target.files); e.target.value = ""; };
let dragDepth = 0;
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
window.addEventListener("dragenter", (e) => { if (hasFiles(e)) { dragDepth++; document.body.classList.add("dragging"); } });
window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove("dragging"); } });
window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
window.addEventListener("drop", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault(); dragDepth = 0; document.body.classList.remove("dragging");
  uploadAll(e.dataTransfer.files);
});

// ---- jobs ----
function renderJobs() {
  const jobs = S.state.jobs;
  $("clear").disabled = !jobs.some((j) => ["done", "failed", "cancelled"].includes(j.status));
  if (!jobs.length) return $("jobs").replaceChildren(h("div", { class: "empty" }, "Nothing yet. Pick videos and a size."));
  $("jobs").replaceChildren(...jobs.map(jobRow));
}
function jobRow(j) {
  const act = (label, fn, cls = "") => h("button", { class: `btn sm ${cls}`, onclick: fn }, label);
  const post = (path) => async () => { try { await api(path, { id: j.id }); poll(); } catch (e) { toast(e.message, true); } };
  const kids = [h("div", { class: "nm", title: j.src }, j.name)];
  const st = [h("span", { class: `badge ${j.status}` }, j.status), h("span", {}, j.gif ? `GIF · ${j.target}` : j.target)];
  if (j.status === "done") {
    const pct = j.in_bytes ? Math.round((1 - j.out_bytes / j.in_bytes) * 100) : 0;
    st.push(h("span", {}, `${human(j.in_bytes)} →`), h("span", { class: "big" }, human(j.out_bytes)),
            h("span", { class: pct >= 0 ? "ok" : "warn" }, pct >= 0 ? `−${pct}%` : `+${-pct}%`));
  } else if (j.status === "running") st.push(h("span", {}, `${j.label} ${Math.round(j.progress * 100)}%`));
  kids.push(h("div", { class: "st" }, st));
  if (j.status === "running" || j.status === "queued")
    kids.push(h("div", { class: "prog" }, h("i", { style: `width:${(j.progress * 100).toFixed(1)}%` })));
  if (j.plan) kids.push(h("div", { class: "muted small" }, j.plan));
  if (j.error) kids.push(h("div", { class: "err" }, j.error));
  const acts = [];
  if (j.status === "done") {
    const dl = h("a", { class: "btn sm", href: media(`/media/${j.id}/out`, "&download=1"), download: j.out_name,
                        draggable: "true", title: "Download, or drag this button to your desktop / Files" }, "Download");
    dl.addEventListener("dragstart", (e) => {
      const mime = j.gif ? "image/gif" : "video/mp4";
      e.dataTransfer.setData("DownloadURL", `${mime}:${j.out_name}:${location.origin}${media(`/media/${j.id}/out`)}`);
    });
    acts.push(act("Preview", () => preview(j), "primary"), dl,
              act("Copy file", async () => { try { await api("/api/copyfile", { id: j.id }); toast("File copied. Paste it into the chat (Ctrl+V)"); } catch (e) { toast(e.message, true); } }),
              act("Copy path", async () => { try { await navigator.clipboard.writeText(j.out); toast("Path copied"); } catch (e) { toast(j.out); } }),
              act("Show in folder", post("/api/reveal")));
  }
  if (j.status === "running" || j.status === "queued") acts.push(act("Cancel", post("/api/cancel"), "danger"));
  else acts.push(act("Remove", post("/api/remove")));
  kids.push(h("div", { class: "acts" }, acts));
  return h("div", { class: "job" }, h("img", { src: thumbUrl(j.src), alt: "", loading: "lazy" }), h("div", {}, kids));
}
$("clear").onclick = async () => { try { await api("/api/clear", {}); poll(); } catch (e) { toast(e.message, true); } };

// ---- preview ----
function modal(...kids) {
  const bg = h("div", { class: "modal-bg", onclick: (e) => { if (e.target === bg) close(); } }, h("div", { class: "modal" }, kids));
  const close = () => { bg.remove(); document.removeEventListener("keydown", esc); };
  const esc = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", esc);
  document.body.append(bg);
  return close;
}
function preview(j) {
  const src = media(`/media/${j.id}/out`);
  let close;
  const el = j.gif ? h("img", { class: "pv", src, alt: j.out_name })
                   : h("video", { src, controls: true, autoplay: true, muted: true, playsinline: true });
  close = modal(h("div", { class: "bar" }, h("b", {}, j.out_name), h("span", { class: "muted" }, human(j.out_bytes)),
                      h("span", { class: "muted small" }, j.plan), h("span", { class: "spacer" }),
                      h("a", { class: "btn sm", href: media(`/media/${j.id}/out`, "&download=1"), download: j.out_name }, "Download"),
                      h("button", { class: "btn sm", onclick: () => close() }, "Close")), el);
}
function previewSource(v) {
  // Source files aren't jobs yet; show the thumbnail large rather than streaming arbitrary paths.
  let close;
  close = modal(h("div", { class: "bar" }, h("b", {}, v.name), h("span", { class: "muted" }, human(v.size)), h("span", { class: "spacer" }),
                      h("button", { class: "btn sm", onclick: () => close() }, "Close")),
                h("img", { class: "pv", src: thumbUrl(v.path), alt: v.name }));
}

// ---- theme ----
const THEMES = ["auto", "light", "dark"];
function themeLabel() { $("theme").textContent = `Theme: ${document.documentElement.dataset.theme || "auto"}`; }
$("theme").onclick = () => {
  const cur = document.documentElement.dataset.theme || "auto";
  const next = THEMES[(THEMES.indexOf(cur) + 1) % 3];
  if (next === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = next;
  try { localStorage.setItem("ulepack-theme", next); } catch (e) { /* blocked */ }
  themeLabel();
};
themeLabel();
poll();
