"""Self-hosted web dashboard for ytmusic-mirror.

Provides a small FastAPI app + single-page HTML so a container can be run like
a "yubal-style" appliance: manage sources, run syncs with live logs, schedule
them, and view what is on disk. It is download-manager only - playback is left
to the user's own media server pointed at the same music folder.

Optional dependency group: `pip install ".[web]"`.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import List, Optional

from fastapi import Body, FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__
from .config import Config, write_default_config
from .core import Logger, pretty_report, sync as run_sync

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
except Exception as _e:  # pragma: no cover - only when [web] not installed
    BackgroundScheduler = None
    CronTrigger = None


_LOG_LIMIT = 2000


class WebSink:
    """Collects progress lines so the UI can tail them."""

    def __init__(self, limit: int = _LOG_LIMIT):
        self.limit = limit
        self.lines: List[str] = []
        self.lock = threading.Lock()

    def write(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)
            if len(self.lines) > self.limit:
                del self.lines[: len(self.lines) - self.limit]

    def tail(self, after: int) -> List[str]:
        with self.lock:
            if after < 0:
                after = 0
            return list(self.lines[after:])


def _sync_runner(cfg: Config, dry_run: bool, sink: WebSink) -> dict:
    """Runs one sync; returns a small summary dict."""
    if dry_run:
        sink.write("DRY RUN - nothing will be downloaded, moved or deleted.\n")
    try:
        report = run_sync(cfg, dry_run=dry_run, log=Logger(out=sink.write))
    except KeyboardInterrupt:
        sink.write("\nInterrupted (safe to re-run).")
        return {"interrupted": True}
    return {
        "dry_run": dry_run,
        "report": pretty_report(report),
        "errors": len(report.errors),
    }


class Server:
    """Holds state shared by HTTP handlers: config, logs, sync, scheduler."""

    def __init__(self, config_path: Path, music_dir_override: Optional[str] = None):
        self.config_path = Path(config_path).expanduser()
        self.sink = WebSink()
        self._sync_active = False
        self._sync_lock = threading.Lock()
        self._last_sync: Optional[dict] = None
        self._ensure_config(music_dir_override)
        self.cfg = Config.load(self.config_path)
        self.scheduler = BackgroundScheduler() if BackgroundScheduler else None
        self._apply_scheduler()

    def _ensure_config(self, music_dir_override: Optional[str]) -> None:
        if self.config_path.is_file():
            return
        music_dir = (
            music_dir_override
            or os.environ.get("YTMUSIC_MIRROR_MUSIC")
            or "~/Music/MP3s"
        )
        write_default_config(self.config_path, music_dir=Path(music_dir))

    def reload(self) -> Config:
        self.cfg = Config.load(self.config_path)
        return self.cfg

    def save(self) -> None:
        self.cfg.save(self.config_path)

    # -- sync -----------------------------------------------------------------

    @property
    def sync_active(self) -> bool:
        return self._sync_active

    def start_sync(self, dry_run: bool) -> bool:
        if not self._sync_lock.acquire(blocking=False):
            return False
        self._sync_active = True
        thread = threading.Thread(
            target=self._run_worker, args=(dry_run,), daemon=True
        )
        thread.start()
        return True

    def _run_worker(self, dry_run: bool) -> None:
        try:
            self.reload()
            self.sink.write(f"\n=== ytmusic-mirror v{__version__} sync "
                            f"{'(dry-run)' if dry_run else ''} started ===")
            self._last_sync = _sync_runner(self.cfg, dry_run, self.sink)
            tail = self._last_sync.get("report") or ""
            if tail:
                self.sink.write(tail)
            if self._last_sync.get("errors"):
                self.sink.write(
                    f"\nSync finished with {self._last_sync['errors']} error(s)."
                )
            else:
                self.sink.write("\nSync finished.")
        except Exception as e:  # noqa: BLE001 - surface everything to the UI
            self.sink.write(f"\nSync crashed: {e}")
            self._last_sync = {"error": str(e)}
        finally:
            self._sync_active = False
            self._sync_lock.release()

    # -- scheduler ------------------------------------------------------------

    def _apply_scheduler(self) -> None:
        if not self.scheduler:
            return
        self.scheduler.remove_all_jobs()
        if not getattr(self, "_scheduler_started", False):
            self.scheduler.start()
            self._scheduler_started = True
        if self.cfg.scheduler_enabled:
            try:
                trigger = CronTrigger.from_crontab(self.cfg.scheduler_cron)
            except ValueError as e:
                self.sink.write(f"Scheduler: invalid cron '{self.cfg.scheduler_cron}': {e}")
                return
            self.scheduler.add_job(
                self._scheduled_sync,
                trigger,
                id="ytmusic-mirror-sync",
                replace_existing=True,
                name="ytmusic-mirror sync",
            )

    def _scheduled_sync(self) -> None:
        self.start_sync(dry_run=False)

    def scheduler_state(self) -> dict:
        if not self.scheduler:
            return {"available": False}
        job = None
        for j in self.scheduler.get_jobs():
            if j.id == "ytmusic-mirror-sync":
                job = j
                break
        return {
            "available": True,
            "enabled": self.cfg.scheduler_enabled,
            "cron": self.cfg.scheduler_cron,
            "next_run": str(job.next_run_time) if job and job.next_run_time else None,
        }

    def set_scheduler(self, enabled: bool, cron: Optional[str]) -> dict:
        self.cfg.scheduler_enabled = bool(enabled)
        if cron:
            self.cfg.scheduler_cron = cron
        self.save()
        self._apply_scheduler()
        return self.scheduler_state()


# --------------------------------------------------------------------------- #
# API helpers
# --------------------------------------------------------------------------- #


def _config_summary(server: Server) -> dict:
    cfg = server.cfg
    return {
        "music_dir": str(cfg.music_dir),
        "channel_url": cfg.channel_url,
        "playlists": list(cfg.playlists),
        "archive_dir": str(cfg.effective_archive_dir),
        "deleted_playlist_policy": cfg.deleted_playlist_policy,
        "orphan_policy": cfg.orphan_policy,
        "remote_components": bool(cfg.remote_components),
        "scheduler": server.scheduler_state(),
    }


def _local_folders(server: Server) -> List[dict]:
    music = server.cfg.music_dir
    folders = []
    if music.is_dir():
        for child in sorted(p for p in music.iterdir() if p.is_dir()):
            if child.name.startswith((".", "_")):
                continue
            songs = 0
            try:
                from .downloader import scan_playlist_folder

                songs = len(scan_playlist_folder(child))
            except Exception:
                songs = -1
            folders.append(
                {
                    "name": child.name,
                    "songs": songs,
                    "config": (child / ".playlist_config.json").is_file(),
                }
            )
    return folders


def create_app(server: Server) -> FastAPI:
    app = FastAPI(title="ytmusic-mirror", version=__version__)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _HTML_PAGE

    @app.get("/api/status")
    def status() -> dict:
        return {
            "version": __version__,
            "running": server.sync_active,
            "config": _config_summary(server),
            "last_sync": server._last_sync,
        }

    @app.get("/api/logs")
    def logs(after: int = 0) -> dict:
        return {
            "running": server.sync_active,
            "seq": len(server.sink.lines),
            "logs": server.sink.tail(after),
        }

    @app.post("/api/discover")
    def discover() -> JSONResponse:
        from .core import discover_remote_playlists

        try:
            found = discover_remote_playlists(server.cfg)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse(
            {
                "remote": [
                    {"id": r.id, "title": r.title, "url": r.url} for r in found
                ]
            }
        )

    @app.post("/api/sources")
    def sources(payload: dict = Body(...)) -> JSONResponse:
        from .cli import _normalize_channel, _normalize_playlist

        action = str(payload.get("action") or "")
        try:
            if action == "set-channel":
                server.cfg.channel_url = _normalize_channel(str(payload.get("url") or ""))
            elif action == "clear-channel":
                server.cfg.channel_url = ""
            elif action == "add-playlist":
                url = _normalize_playlist(str(payload.get("url") or ""))
                server.cfg.playlists.append(url)
            elif action == "remove-playlist":
                server.cfg.playlists = [
                    u for u in server.cfg.playlists if u != str(payload.get("url") or "")
                ]
            else:
                return JSONResponse({"error": f"unknown action: {action}"}, status_code=400)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        server.save()
        return JSONResponse(_config_summary(server))

    @app.patch("/api/settings")
    def settings(payload: dict = Body(...)) -> JSONResponse:
        cfg = server.cfg
        for key in ("orphan_policy", "deleted_playlist_policy"):
            if key in payload and str(payload[key]) in (
                "smart", "archive", "delete", "keep",
            ):
                setattr(cfg, key, str(payload[key]))
        if "remote_components" in payload:
            cfg.remote_components = (
                ["ejs:github"] if bool(payload["remote_components"]) else []
            )
        if "scheduler" in payload and isinstance(payload["scheduler"], dict):
            sched = payload["scheduler"]
            if "enabled" in sched:
                server.cfg.scheduler_enabled = bool(sched["enabled"])
            if "cron" in sched:
                server.cfg.scheduler_cron = str(sched["cron"])
            server.save()
            state = server.set_scheduler(
                server.cfg.scheduler_enabled, server.cfg.scheduler_cron
            )
            return JSONResponse({**_config_summary(server), "scheduler": state})
        server.save()
        return JSONResponse(_config_summary(server))

    @app.post("/api/sync")
    def sync_now(payload: dict = Body(default={})) -> JSONResponse:
        dry_run = bool(payload.get("dry_run"))
        if not server.start_sync(dry_run):
            return JSONResponse({"error": "a sync is already running"}, status_code=409)
        return JSONResponse({"started": True, "dry_run": dry_run})

    @app.get("/api/local")
    def local() -> dict:
        return {"folders": _local_folders(server), "music_dir": str(server.cfg.music_dir)}

    return app


# --------------------------------------------------------------------------- #
# Single-page UI
# --------------------------------------------------------------------------- #

_HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ytmusic-mirror</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; max-width: 860px; margin: 0 auto; padding: 16px; }
  h1 { font-size: 1.3rem; }
  h2 { font-size: 1rem; border-bottom: 1px solid #8883; padding-bottom: 4px; }
  pre, #log { background: #0003; border-radius: 6px; padding: 8px; overflow: auto;
              white-space: pre-wrap; font-size: .82rem; max-height: 340px; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; }
  input[type=text] { flex: 1; min-width: 220px; }
  button { cursor: pointer; }
  .card { border: 1px solid #8885; border-radius: 8px; padding: 12px; margin: 10px 0; }
  .muted { color: #888; font-size: .8rem; }
  .badge { display: inline-block; border-radius: 999px; padding: 0 8px;
           font-size: .75rem; background: #0a8; color: #fff; }
  .badge.off { background: #777; }
  ul { margin: 4px 0; padding-left: 20px; }
  li button { margin-left: 8px; }
</style>
</head>
<body>
<h1>ytmusic-mirror <span id="ver" class="muted"></span>
  <span id="running" class="badge off">idle</span></h1>
<div id="status" class="card"></div>

<div class="card">
  <h2>Sources</h2>
  <div class="row">
    <input type="text" id="channelInput" placeholder="Channel URL or @handle">
    <button onclick="channel('set')">Set channel</button>
    <button onclick="channel('clear')">Clear</button>
  </div>
  <div class="row" style="margin-top:8px">
    <input type="text" id="playlistInput" placeholder="Playlist URL to add">
    <button onclick="addPlaylist()">Add playlist</button>
    <button onclick="discover()">Discover channel playlists</button>
  </div>
  <ul id="sourceList"></ul>
  <div id="discoverBox"></div>
</div>

<div class="card">
  <h2>Sync</h2>
  <div class="row">
    <label><input type="checkbox" id="dryRun"> dry run</label>
    <button id="syncBtn" onclick="runSync()">Run sync</button>
    <span id="syncMsg" class="muted"></span>
  </div>
  <div id="report" class="card" style="display:none"></div>
</div>

<div class="card">
  <h2>Live log</h2>
  <pre id="log">(no output yet)</pre>
</div>

<div class="card">
  <h2>On disk</h2>
  <ul id="localList"></ul>
</div>

<div class="card">
  <h2>Settings</h2>
  <div class="row">
    <label>Orphan policy
      <select id="orphan"><option>smart</option><option>archive</option><option>delete</option></select>
    </label>
    <label>Deleted playlist
      <select id="deleted"><option>archive</option><option>delete</option><option>keep</option></select>
    </label>
    <label><input type="checkbox" id="remoteComponents"> remote_components (EJS solver)</label>
  </div>
  <div class="row" style="margin-top:8px">
    <label><input type="checkbox" id="schedEnabled"> Schedule sync</label>
    <input type="text" id="schedCron" value="0 0 * * *" style="max-width:180px">
    <button onclick="saveSettings()">Save settings</button>
    <span id="nextRun" class="muted"></span>
  </div>
</div>

<script>
let seq = 0, last = null;

async function api(path, opts={}) {
  const r = await fetch(path, Object.assign({headers:{'Content-Type':'application/json'}}, opts));
  const j = await r.json().catch(()=> ({}));
  if (!r.ok) throw new Error(j.error || (r.status + ' ' + r.statusText));
  return j;
}
function el(id){ return document.getElementById(id); }
function logLine(l){ const box = el('log'); box.textContent = box.textContent + l + '\\n';
  box.scrollTop = box.scrollHeight; }

function renderStatus(s){
  el('ver').textContent = 'v' + s.version;
  const cfg = s.config, badge = el('running');
  badge.textContent = s.running ? 'syncing…' : 'idle';
  badge.className = 'badge' + (s.running ? '' : ' off');
  el('status').innerHTML =
    '<b>Master folder:</b> <code>' + cfg.music_dir + '</code><br>' +
    '<b>Channel:</b> ' + (cfg.channel_url || '<span class="muted">not set</span>') +
    ' &nbsp; <b>Archive:</b> ' + cfg.archive_dir;
  el('orphan').value = cfg.orphan_policy;
  el('deleted').value = cfg.deleted_playlist_policy;
  el('remoteComponents').checked = cfg.remote_components;
  el('schedEnabled').checked = cfg.scheduler.enabled;
  el('schedCron').value = cfg.scheduler.cron;
  el('nextRun').textContent = cfg.scheduler.next_run ? ('next: ' + cfg.scheduler.next_run) : '';
  const ul = el('sourceList'); ul.innerHTML = '';
  cfg.playlists.forEach(u => {
    const li = document.createElement('li');
    li.textContent = u;
    const b = document.createElement('button'); b.textContent='remove';
    b.onclick = ()=> source('remove-playlist', u);
    li.appendChild(b); ul.appendChild(li);
  });
  if (s.last_sync && s.last_sync.report) showReport(s.last_sync.report);
}
function showReport(text){
  el('report').style.display = 'block';
  el('report').textContent = text;
}

async function channel(action){
  try {
    const url = action === 'set' ? el('channelInput').value : '';
    await api('/api/sources', {method:'POST', body: JSON.stringify({action: action==='set'?'set-channel':'clear-channel', url})});
    await refresh();
  } catch(e){ alert(e.message); }
}
async function addPlaylist(){
  try {
    const url = el('playlistInput').value;
    if (!url) return;
    await api('/api/sources', {method:'POST', body: JSON.stringify({action:'add-playlist', url})});
    el('playlistInput').value = '';
    await refresh();
  } catch(e){ alert(e.message); }
}
async function source(action, url){
  try {
    await api('/api/sources', {method:'POST', body: JSON.stringify({action, url})});
    await refresh();
  } catch(e){ alert(e.message); }
}
async function discover(){
  const box = el('discoverBox'); box.innerHTML = 'discovering…';
  try {
    const j = await api('/api/discover', {method:'POST'});
    box.innerHTML = '<ul>' + (j.remote||[]).map(r =>
      '<li>' + r.title + ' <button onclick="source(\'add-playlist\', \'' + r.url + '\')">add</button></li>'
    ).join('') + '</ul>';
  } catch(e){ box.textContent = e.message; }
}
async function runSync(){
  const btn = el('syncBtn'); btn.disabled = true;
  try {
    await api('/api/sync', {method:'POST', body: JSON.stringify({dry_run: el('dryRun').checked})});
  } catch(e){ alert(e.message); }
}
async function saveSettings(){
  try {
    await api('/api/settings', {method:'PATCH', body: JSON.stringify({
      orphan_policy: el('orphan').value,
      deleted_playlist_policy: el('deleted').value,
      remote_components: el('remoteComponents').checked,
      scheduler: {enabled: el('schedEnabled').checked, cron: el('schedCron').value}
    })});
    await refresh();
  } catch(e){ alert(e.message); }
}
async function refresh(){
  const s = await api('/api/status');
  renderStatus(s);
  const l = await api('/api/local');
  const ul = el('localList'); ul.innerHTML = '';
  l.folders.forEach(f => {
    const li = document.createElement('li');
    li.textContent = f.name + ' — ' + (f.songs >= 0 ? f.songs + ' songs' : '?') + (f.config ? '' : ' (no config)');
    ul.appendChild(li);
  });
}
async function tail(){
  try {
    const l = await api('/api/logs?after=' + seq);
    seq = l.seq;
    if (l.logs.length) logLine(l.logs.join('\\n'));
    if (el('running').textContent === 'syncing…' && !l.running){
      el('syncBtn').disabled = false; el('syncMsg').textContent = '';
      const s = await api('/api/status'); renderStatus(s); await refresh();
    }
  } catch(_){}
}
refresh().catch(e=>alert(e));
setInterval(tail, 900);
setInterval(refresh, 4000);
</script>
</body>
</html>
"""


def run(config_path: Path, music_dir: Optional[str] = None,
        host: str = "127.0.0.1", port: int = 8000) -> None:
    """Serve the dashboard (blocking)."""
    import uvicorn

    server = Server(config_path, music_dir)
    app = create_app(server)
    if server.cfg.scheduler_enabled:
        server.sink.write(
            f"Scheduler enabled: {server.cfg.scheduler_cron} "
            f"(next run {server.scheduler_state()['next_run']})"
        )
    print(f"ytmusic-mirror dashboard on http://{host}:{port}")
    print(f"Config : {server.config_path}")
    print(f"Music  : {server.cfg.music_dir}")
    if host in ("0.0.0.0", "::"):
        print("WARNING: bound to all interfaces - no authentication is built in!")
    uvicorn.run(app, host=host, port=port, log_level="warning")
