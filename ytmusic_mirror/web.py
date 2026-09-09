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


def _sync_runner(cfg: Config, dry_run: bool, sink: WebSink,
                 cancel=None) -> dict:
    """Runs one sync; returns a small summary dict."""
    if dry_run:
        sink.write("DRY RUN - nothing will be downloaded, moved or deleted.\n")
    try:
        report = run_sync(cfg, dry_run=dry_run, log=Logger(out=sink.write),
                          cancel=cancel)
    except KeyboardInterrupt:
        sink.write("\nInterrupted (safe to re-run).")
        return {"interrupted": True}
    return {
        "dry_run": dry_run,
        "report": pretty_report(report),
        "errors": len(report.errors),
        "cancelled": bool(report.cancelled),
    }


class Server:
    """Holds state shared by HTTP handlers: config, logs, sync, scheduler."""

    def __init__(self, config_path: Path, music_dir_override: Optional[str] = None):
        self.config_path = Path(config_path).expanduser()
        self.sink = WebSink()
        self._sync_active = False
        self._sync_lock = threading.Lock()
        self._cancel_event = threading.Event()
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
        self._cancel_event.clear()
        self._sync_active = True
        thread = threading.Thread(
            target=self._run_worker, args=(dry_run,), daemon=True
        )
        thread.start()
        return True

    def request_stop(self) -> None:
        self._cancel_event.set()
        self.sink.write("\nStop requested - finishing current step, will not "
                        "start new playlists.")

    def _run_worker(self, dry_run: bool) -> None:
        try:
            self.reload()
            self.sink.write(f"\n=== ytmusic-mirror v{__version__} sync "
                            f"{'(dry-run)' if dry_run else ''} started ===")
            self._last_sync = _sync_runner(
                self.cfg, dry_run, self.sink,
                cancel=lambda: self._cancel_event.is_set(),
            )
            if self._last_sync.get("cancelled"):
                self.sink.write("\nSync stopped by user - already-finished "
                                "playlists are kept; re-run to continue.")
            tail = self._last_sync.get("report") or ""
            if tail:
                self.sink.write(tail)
            if self._last_sync.get("errors"):
                self.sink.write(
                    f"\nSync finished with {self._last_sync['errors']} error(s)."
                )
            elif not self._last_sync.get("cancelled"):
                self.sink.write("\nSync finished.")
        except Exception as e:  # noqa: BLE001 - surface everything to the UI
            self.sink.write(f"\nSync crashed: {e}")
            self._last_sync = {"error": str(e)}
        finally:
            self._sync_active = False
            self._sync_lock.release()

    def set_music_dir(self, raw: str) -> tuple:
        """Change the master folder; returns (ok, error_or_none)."""
        from .config import expand_user_path

        candidate = raw.strip()
        absolute = candidate.startswith(("~", "/")) or (
            len(candidate) >= 2 and candidate[1] == ":"
        )
        if not absolute:
            return False, "Use an absolute path or ~/... (e.g. /music)"
        try:
            new_dir = expand_user_path(candidate)
        except ValueError as e:
            return False, str(e)
        self.cfg.music_dir = new_dir
        self.save()
        self.reload()
        self.sink.write(f"Master folder changed to {new_dir}")
        return True, None

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
                if url not in server.cfg.playlists:
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

    @app.post("/api/sync/stop")
    def sync_stop() -> JSONResponse:
        if not server.sync_active:
            return JSONResponse({"stopping": False})
        server.request_stop()
        return JSONResponse({"stopping": True})

    @app.post("/api/folder")
    def folder(payload: dict = Body(...)) -> JSONResponse:
        raw = str(payload.get("path") or "")
        if not raw:
            return JSONResponse({"error": "empty path"}, status_code=400)
        ok, error = server.set_music_dir(raw)
        if not ok:
            return JSONResponse({"error": error}, status_code=400)
        return JSONResponse(_config_summary(server))

    @app.get("/api/local")
    def local() -> dict:
        return {"folders": _local_folders(server), "music_dir": str(server.cfg.music_dir)}

    return app


# --------------------------------------------------------------------------- #
# Single-page UI
# --------------------------------------------------------------------------- #

_HTML_PAGE = (Path(__file__).with_name("ui.html")).read_text(encoding="utf-8")


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
