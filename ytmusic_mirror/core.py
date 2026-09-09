"""Core sync engine for ytmusic-mirror.

Responsibilities:
  * Discover the set of remote public playlists (channel tab + explicit list).
  * Map local playlist folders to playlists via the downloader's config file.
  * Create folders for new playlists, rename folders for renames, and archive
    folders whose playlist disappeared from the account.
  * Delegate per-song download / reorder / retention to our native downloader
    module, then enforce removal semantics: songs removed from a playlist that
    are still on YouTube are deleted locally, while songs whose video has been
    delisted are moved into an archive folder.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import yt_dlp

from . import downloader
from .config import CONFIG_FILE_NAME, Config

_VIDEO_ID_LEN = 11
_PLAYLIST_ID_PREFIX = re.compile(
    r"^(PL|OLAK5uy_|RD|RDEM|RDCLAK5uy_|LM|LL|FL|UU|PU|TL|VL|OL|LA|EC|PLRD|TLRD)"
)
_UNAVAILABLE_TITLES = (
    "[Private video]",
    "[Deleted video]",
    "[Video unavailable]",
    "(Not available)",
)
SIDECAR_NAME = ".ytmusic-mirror.json"


@dataclass
class RemotePlaylist:
    id: str
    title: str
    url: str


@dataclass
class LocalPlaylist:
    folder: str
    config_path: Path
    raw_config: dict


@dataclass
class PlaylistPlan:
    remote_id: str
    remote_title: str
    remote_url: str
    action: str  # create | update | archive_deleted
    folder: str = ""
    new_songs: int = 0
    removed_songs: int = 0
    delisted_songs: int = 0
    unavailable: int = 0
    rename_to: str = ""


@dataclass
class SyncReport:
    created: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    renamed: List[str] = field(default_factory=list)
    deleted_playlists: List[str] = field(default_factory=list)
    archived_playlists: List[str] = field(default_factory=list)
    removed_songs: List[str] = field(default_factory=list)
    delisted_songs: List[str] = field(default_factory=list)
    unavailable: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    cancelled: bool = False
    errors: List[str] = field(default_factory=list)
    skipped_playlists: List[str] = field(default_factory=list)


class Logger:
    """Streams progress/info to stdout (unless quiet) and warnings to stderr."""

    def __init__(self, out: Callable[[str], None] = None, quiet: bool = False):
        self._out = out or (lambda message: print(message))
        self.quiet = quiet

    def info(self, message: str) -> None:
        if not self.quiet:
            self._out(message)

    def warn(self, message: str) -> None:
        if not self.quiet:
            print(f"[warn] {message}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def playlist_id_from_url(url: str) -> Optional[str]:
    query = parse_qs(urlparse(url).query)
    values = query.get("list")
    if values:
        return values[0]
    return None


def playlist_url_from_id(playlist_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={playlist_id}"


def _ytdl_base_opts(cfg: Config) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "cookiefile": None if not cfg.cookie_file else str(Path(cfg.cookie_file).expanduser()),
        "cookiesfrombrowser": None if not cfg.cookies_from_browser else (cfg.cookies_from_browser,),
    }
    return opts


def _flat_opts(cfg: Config) -> dict:
    opts = _ytdl_base_opts(cfg)
    opts.update({"extract_flat": True, "skip_download": True})
    return opts


def _channel_playlists_tab_url(channel_url: str) -> str:
    url = channel_url.strip()
    path = urlparse(url).path.rstrip("/")
    if path.endswith("/playlists"):
        return url
    return f"{url.rstrip('/')}/playlists"


def enumerate_channel_playlists(channel_url: str, cfg: Config) -> List[RemotePlaylist]:
    """Return the public playlists listed on a channel's /playlists tab."""
    url = _channel_playlists_tab_url(channel_url)
    if not urlparse(url).scheme:
        url = "https://" + url

    found: List[RemotePlaylist] = []
    with yt_dlp.YoutubeDL(_flat_opts(cfg)) as ydl:
        info = ydl.extract_info(url, download=False)
    for entry in info.get("entries") or []:
        if not entry or not entry.get("id"):
            continue
        playlist_id = str(entry["id"])
        title = entry.get("title") or playlist_id
        # Playlist ids from a channel tab are playlist URLs, not watch pages.
        if not _looks_like_playlist_id(playlist_id):
            continue
        found.append(RemotePlaylist(playlist_id, str(title), playlist_url_from_id(playlist_id)))
    return found


def _looks_like_playlist_id(playlist_id: str) -> bool:
    if _PLAYLIST_ID_PREFIX.match(playlist_id):
        return True
    return len(playlist_id) != _VIDEO_ID_LEN


def discover_remote_playlists(cfg: Config) -> List[RemotePlaylist]:
    """Discover remote playlists from the channel tab plus any explicit list."""
    combined: List[RemotePlaylist] = []
    seen: set = set()

    if cfg.channel_url:
        for rp in enumerate_channel_playlists(cfg.channel_url, cfg):
            if rp.id in seen:
                continue
            seen.add(rp.id)
            combined.append(rp)

    for url in cfg.playlists:
        url = url.strip()
        if not url:
            continue
        playlist_id = playlist_id_from_url(url)
        if playlist_id is None:
            raise ValueError(f"Not a playlist URL (no ?list= param): {url}")
        if playlist_id in seen:
            continue
        title = _fetch_playlist_title(url, cfg)
        seen.add(playlist_id)
        combined.append(RemotePlaylist(playlist_id, title, url))

    return combined


def _fetch_playlist_title(url: str, cfg: Config) -> str:
    settings = downloader.new_settings(cfg, url)
    try:
        return downloader.fetch_playlist(url, settings).get("title") or url
    except Exception:
        return url


def _remote_entries(cfg: Config, url: str) -> List[dict]:
    """Fetch a remote playlist once, returning {id,title,available} dicts."""
    settings = downloader.new_settings(cfg, url)
    try:
        data = downloader.fetch_playlist(url, settings)
    except Exception:
        return []
    return data.get("entries") or []


def _read_unavailable_notes(folder: Path) -> Dict[str, dict]:
    """Read the per-playlist record of listed-but-unavailable video ids."""
    sidecar = folder / SIDECAR_NAME
    if sidecar.is_file():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            notes = data.get("unavailable") or {}
            return {str(k): v for k, v in notes.items() if isinstance(v, dict)}
        except Exception:
            return {}
    return {}


def _write_unavailable_notes(folder: Path, notes: Dict[str, dict]) -> None:
    sidecar = folder / SIDECAR_NAME
    data = {"version": 1, "unavailable": notes}
    sidecar.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _probe_video_available(video_id: str, cfg: Config) -> bool:
    """Return False when the video has been removed/privated/delisted."""
    opts = _flat_opts(cfg)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
        if not info:
            return False
        if info.get("channel_id") is None:
            return False
        title = str(info.get("title") or "").strip()
        for marker in _UNAVAILABLE_TITLES:
            if title.startswith(marker):
                return False
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Local snapshot
# --------------------------------------------------------------------------- #


def snapshot_local_playlists(music_dir: Path) -> Dict[str, LocalPlaylist]:
    """Map playlist id -> folder for every managed folder under music_dir."""
    local: Dict[str, LocalPlaylist] = {}
    if not music_dir.is_dir():
        return local

    for child in sorted(music_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith((".", "_")):
            continue
        config_file = child / CONFIG_FILE_NAME
        if not config_file.is_file():
            continue
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            raise RuntimeError(f"Invalid config file '{config_file}': {e}") from e
        playlist_id = playlist_id_from_url(str(raw.get("url") or ""))
        if playlist_id is None:
            raise RuntimeError(
                f"Config file '{config_file}' has an invalid playlist URL; "
                "fix or remove it before syncing."
            )
        if playlist_id in local:
            raise RuntimeError(
                f"Duplicate local folders for playlist '{playlist_id}': "
                f"'{local[playlist_id].folder}' and '{child.name}'."
            )
        local[playlist_id] = LocalPlaylist(child.name, config_file, raw)
    return local


# --------------------------------------------------------------------------- #
# Sync
# --------------------------------------------------------------------------- #


def sync(
    cfg: Config,
    dry_run: bool = False,
    log: Optional[Logger] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> SyncReport:
    """Mirror the configured remote playlists into the music directory.

    Designed to be resumable: per-playlist work is isolated so one failure or an
    interrupted run never loses finished work - simply run it again to continue.
    When `cancel()` returns True the sync stops at the next playlist boundary.
    """
    log = log or Logger()
    report = SyncReport()
    previous_cwd = Path.cwd()
    try:
        _sync_locked(cfg, dry_run=dry_run, report=report, log=log, cancel=cancel)
    finally:
        if Path.cwd() != previous_cwd:
            os.chdir(previous_cwd)
    return report


def _sync_locked(
    cfg: Config,
    dry_run: bool,
    report: SyncReport,
    log: Logger,
    cancel: Optional[Callable[[], bool]] = None,
) -> None:
    """Body of sync(), executed with the music dir as the working directory."""
    cfg.music_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(cfg.music_dir)

    try:
        remote = discover_remote_playlists(cfg)
    except Exception as e:
        report.errors.append(f"Playlist discovery failed: {e}")
        return

    if not remote and not cfg.channel_url and not cfg.playlists:
        report.errors.append(
            "No playlist source configured. Set 'channel_url' or add playlist "
            "URLs to 'playlists' in the config file."
        )
        return

    if not remote:
        log.warn(
            "No remote playlists found. Existing local folders will be "
            "archived/deleted according to 'deleted_playlist_policy'."
        )

    remote_by_id = {rp.id: rp for rp in remote}
    local = snapshot_local_playlists(cfg.music_dir)

    total = len(remote) + len([i for i in local if i not in remote_by_id])
    processed = 0

    # Order: keep channel/remote ordering, then handle disappeared folders.
    for rp in remote:
        processed += 1
        local_pl = local.get(rp.id)
        folder = local_pl.folder if local_pl else ""
        action = "update" if local_pl else "create"
        plan = PlaylistPlan(rp.id, rp.title, rp.url, action, folder=folder)
        _execute_plan(
            cfg, plan, dry_run=dry_run, report=report, log=log,
            seq=(processed, total),
        )
        if cancel and cancel():
            log.info("Sync stopped by request (next sync will resume).")
            report.cancelled = True
            break

    if cancel and cancel():
        log.info("Sync stopped by request (next sync will resume).")
        report.cancelled = True

    for playlist_id, local_pl in sorted(local.items()):
        if playlist_id in remote_by_id:
            continue
        processed += 1
        plan = PlaylistPlan(
            remote_id=playlist_id,
            remote_title=local_pl.folder,
            remote_url=str(local_pl.raw_config.get("url") or ""),
            action="archive_deleted",
            folder=local_pl.folder,
        )
        _execute_plan(
            cfg, plan, dry_run=dry_run, report=report, log=log,
            seq=(processed, total),
        )
        if cancel and cancel():
            log.info("Sync stopped by request (next sync will resume).")
            report.cancelled = True
            break


def _execute_plan(
    cfg: Config,
    plan: PlaylistPlan,
    dry_run: bool,
    report: SyncReport,
    log: Logger,
    seq: Optional[tuple] = None,
) -> None:
    prefix = f"[{seq[0]}/{seq[1]}] " if seq else ""

    if plan.action == "archive_deleted":
        _handle_deleted_playlist(cfg, plan, dry_run, report, log)
        return

    expected_folder = downloader.sanitize_name(plan.remote_title)
    if plan.action == "update" and plan.folder != expected_folder:
        plan.rename_to = expected_folder
        report.renamed.append(f"{plan.folder} -> {expected_folder}")

    # Load/create our own per-playlist settings (identity + resume state).
    if plan.action == "create":
        settings = downloader.new_settings(cfg, plan.remote_url)
        folder_abs = cfg.music_dir / expected_folder
        local_files = {}
    else:
        config_file = cfg.music_dir / plan.folder / CONFIG_FILE_NAME
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            report.errors.append(f"Playlist '{plan.remote_title}' skipped: {e}")
            return
        settings = downloader.settings_from_config(raw)
        settings["url"] = plan.remote_url
        _merge_cookies(cfg, settings)
        folder_abs = cfg.music_dir / plan.folder
        try:
            local_files = downloader.scan_playlist_folder(folder_abs)
        except Exception as e:
            report.errors.append(f"Playlist '{plan.remote_title}' skipped: {e}")
            report.skipped_playlists.append(plan.remote_title)
            return

    # Fetch remote song ids to compute add/remove deltas. Entries that are
    # listed but unavailable are tracked and never counted as "new" forever.
    entries = _remote_entries(cfg, plan.remote_url)
    remote_id_set = {e["id"] for e in entries}
    unavailable_ids = {e["id"] for e in entries if not e["available"]}
    entry_titles = {e["id"]: e["title"] for e in entries}

    local_id_set = set(local_files.keys())
    orphan_ids = sorted(local_id_set - remote_id_set)
    available_new = sorted((remote_id_set - unavailable_ids) - local_id_set)
    unavailable_new = sorted(unavailable_ids - local_id_set)
    plan.new_songs = len(available_new)
    plan.removed_songs = len(orphan_ids)
    plan.unavailable = len(unavailable_new)

    if plan.action == "create":
        report.created.append(plan.remote_title)
    else:
        report.updated.append(plan.remote_title)

    if dry_run:
        _classify_orphans_dry(cfg, plan, orphan_ids, report)
        message = (
            f"{prefix}[dry-run] {plan.action}: '{plan.remote_title}'"
            f" (+{plan.new_songs} new, {plan.removed_songs} to reconcile)"
            + (f" rename folder to '{plan.rename_to}'" if plan.rename_to else "")
        )
        if plan.unavailable:
            message += f", {plan.unavailable} listed-unavailable (will be noted)"
        log.info(message)
        return

    # Folder-level changes (create / rename) and the per-playlist config file.
    if plan.action == "create":
        folder_abs.mkdir(parents=True, exist_ok=True)
    elif plan.rename_to:
        old_abs = cfg.music_dir / plan.folder
        try:
            old_abs.rename(cfg.music_dir / expected_folder)
            folder_abs = cfg.music_dir / expected_folder
            plan.folder = expected_folder
        except OSError as e:
            report.errors.append(f"Could not rename '{plan.folder}': {e}")
            return
        # Refresh the album tag so it follows the renamed playlist.
        include = settings.get("include_metadata", {})
        if include.get("album") and settings.get("use_playlist_name"):
            for song in downloader.scan_playlist_folder(folder_abs).values():
                try:
                    downloader.update_album_tag(song.file_path, plan.remote_title)
                except Exception as e:
                    report.warnings.append(f"Could not retag '{song.file_name}': {e}")
    downloader.write_playlist_config(folder_abs, settings)

    # Reconcile orphaned songs first so the folder mirrors the live playlist,
    # then let the downloader add new songs and reorder what remains.
    if orphan_ids:
        _reconcile_orphans(cfg, plan, orphan_ids, report, log)

    log.info(
        f"{prefix}{'Creating' if plan.action == 'create' else 'Updating'} "
        f"playlist '{plan.remote_title}'... (+{plan.new_songs} new)"
    )
    try:
        _clean_temp_files(folder_abs)
        result = downloader.sync_playlist(
            folder_abs,
            plan.remote_title,
            entries,
            settings,
            log=lambda m: log.info(f"{prefix}{m}"),
        )
        for message in result["failed"]:
            report.warnings.append(f"{plan.remote_title}: {message}")
        log.info(f"{prefix}Finished '{plan.remote_title}' "
                 f"(+{result['added']} new, {len(result['failed'])} failed).")
    except Exception as e:
        report.errors.append(f"Playlist '{plan.remote_title}' failed: {e}")

    if unavailable_new:
        _note_unavailable_songs(
            plan, folder_abs, unavailable_new, entry_titles,
            unavailable_ids, report,
        )
        log.info(
            f"{prefix}{len(unavailable_new)} song(s) listed as unavailable - "
            f"recorded in '{folder_abs.name}/{SIDECAR_NAME}'. They stay noted "
            f"(and are retried) until they become downloadable."
        )


def _note_unavailable_songs(
    plan: PlaylistPlan,
    folder_abs: Path,
    unavailable_new: List[str],
    entry_titles: Dict[str, str],
    currently_unavailable: set,
    report: SyncReport,
) -> None:
    """Persist listed-but-unavailable ids and drop notes that are no longer valid."""
    if not folder_abs.is_dir():
        return
    notes = _read_unavailable_notes(folder_abs)
    changed = False
    now = datetime.now().isoformat(timespec="seconds")
    for video_id in unavailable_new:
        if video_id not in notes:
            notes[video_id] = {
                "first_seen": now,
                "title": entry_titles.get(video_id, ""),
            }
            label = f"{plan.remote_title}: {video_id}"
            if entry_titles.get(video_id):
                label += f" ({entry_titles[video_id]})"
            report.unavailable.append(label)
            changed = True
    # Prune: entry downloaded, no longer listed, or available again.
    try:
        present = set(downloader.scan_playlist_folder(folder_abs).keys())
    except Exception:
        present = set()
    for video_id in list(notes):
        if video_id in present or video_id not in currently_unavailable:
            del notes[video_id]
            changed = True
    if changed:
        try:
            _write_unavailable_notes(folder_abs, notes)
        except OSError as e:
            report.errors.append(f"Could not write {SIDECAR_NAME}: {e}")


_TEMP_SUFFIXES = (".part", ".ytdl", ".temp", ".tmp")


def _clean_temp_files(folder: Path) -> None:
    """Remove interrupted-download leftovers so a re-run starts clean."""
    if not folder.is_dir():
        return
    for child in folder.iterdir():
        if child.is_file() and child.name.lower().endswith(_TEMP_SUFFIXES):
            try:
                child.unlink()
            except OSError:
                pass


def _merge_cookies(cfg: Config, config: dict) -> None:
    if not config.get("cookies_from_browser") and cfg.cookies_from_browser:
        config["cookies_from_browser"] = cfg.cookies_from_browser
    if not config.get("cookie_file") and cfg.cookie_file:
        config["cookie_file"] = cfg.cookie_file
    if not config.get("remote_components") and cfg.remote_components:
        config["remote_components"] = list(cfg.remote_components)


def _handle_deleted_playlist(
    cfg: Config,
    plan: PlaylistPlan,
    dry_run: bool,
    report: SyncReport,
    log: Logger,
) -> None:
    policy = cfg.deleted_playlist_policy
    source = cfg.music_dir / plan.folder
    if not source.is_dir():
        return
    if policy == "keep":
        log.info(f"Playlist '{plan.folder}' no longer in account; keeping local folder.")
        return
    if policy == "delete":
        report.deleted_playlists.append(plan.folder)
        if dry_run:
            log.info(f"[dry-run] delete folder '{plan.folder}'")
            return
        log.info(f"Deleting folder '{plan.folder}' (playlist removed from account)...")
        try:
            shutil.rmtree(source)
        except Exception as e:
            report.errors.append(f"Could not delete '{plan.folder}': {e}")
        return
    # archive (default)
    report.archived_playlists.append(plan.folder)
    dest = cfg.effective_archive_dir / "deleted" / plan.remote_id
    if dry_run:
        log.info(f"[dry-run] archive folder '{plan.folder}' -> {dest}")
        return
    log.info(f"Archiving folder '{plan.folder}' (playlist removed from account)...")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(dest))
    except Exception as e:
        report.errors.append(f"Could not archive '{plan.folder}': {e}")


def _classify_orphans_dry(
    cfg: Config, plan: PlaylistPlan, orphan_ids: List[str], report: SyncReport
) -> None:
    for video_id in orphan_ids:
        if cfg.orphan_policy == "archive":
            report.delisted_songs.append(f"{plan.remote_title}: {video_id}")
        elif cfg.orphan_policy == "delete":
            report.removed_songs.append(f"{plan.remote_title}: {video_id}")
        else:  # smart
            if _probe_video_available(video_id, cfg):
                report.removed_songs.append(f"{plan.remote_title}: {video_id}")
            else:
                report.delisted_songs.append(f"{plan.remote_title}: {video_id}")


def _reconcile_orphans(
    cfg: Config,
    plan: PlaylistPlan,
    orphan_ids: List[str],
    report: SyncReport,
    log: Logger,
) -> None:
    """Delete user-removed songs; archive songs whose video has been delisted."""
    folder_abs = cfg.music_dir / plan.folder
    try:
        local_files = downloader.scan_playlist_folder(folder_abs)
    except Exception as e:
        report.errors.append(f"Could not scan '{plan.folder}' for cleanup: {e}")
        return

    for video_id in orphan_ids:
        info = local_files.get(video_id)
        if info is None:
            continue
        file_path = info.file_path
        file_name = info.file_name

        if cfg.orphan_policy == "archive":
            available = False
            delisted = True
        elif cfg.orphan_policy == "delete":
            available = True
            delisted = False
        else:  # smart
            available = _probe_video_available(video_id, cfg)
            delisted = not available

        if available and not delisted:
            report.removed_songs.append(f"{plan.remote_title}: {file_name}")
            log.info(f"Deleting '{file_name}' (removed from playlist, still on YouTube)...")
            try:
                file_path.unlink()
            except Exception as e:
                report.errors.append(f"Could not delete '{file_path}': {e}")
        elif delisted:
            report.delisted_songs.append(f"{plan.remote_title}: {file_name}")
            dest = cfg.effective_archive_dir / "delisted" / plan.remote_id
            dest.mkdir(parents=True, exist_ok=True)
            log.info(f"Archiving '{file_name}' (video no longer on YouTube)...")
            try:
                shutil.move(str(file_path), str(dest / file_name))
            except Exception as e:
                report.errors.append(f"Could not archive '{file_path}': {e}")


def _section(lines: list, header: str, items: List[str]) -> None:
    if items:
        lines.append(f"{header} ({len(items)}):")
        lines.extend(f"  - {item}" for item in items)


def pretty_report(report: SyncReport) -> str:
    lines: List[str] = []
    _section(lines, "Created playlists", report.created)
    _section(lines, "Renamed playlists", report.renamed)
    _section(lines, "Updated playlists", report.updated)
    _section(lines, "Archived playlists", report.archived_playlists)
    _section(lines, "Deleted playlists", report.deleted_playlists)
    if report.removed_songs:
        lines.append(f"Songs removed from playlists (deleted locally) ({len(report.removed_songs)}):")
        lines.extend(f"  - {x}" for x in report.removed_songs)
    if report.delisted_songs:
        lines.append(f"Songs delisted on YouTube (archived) ({len(report.delisted_songs)}):")
        lines.extend(f"  - {x}" for x in report.delisted_songs)
    if report.unavailable:
        lines.append(f"Songs listed as unavailable (recorded, not downloaded) ({len(report.unavailable)}):")
        lines.extend(f"  - {x}" for x in report.unavailable)
    _section(lines, "Skipped playlists", report.skipped_playlists)
    if report.warnings:
        lines.append("Warnings:")
        lines.extend(f"  - {x}" for x in report.warnings)
    if report.errors:
        lines.append("Errors:")
        lines.extend(f"  ! {x}" for x in report.errors)
    return "\n".join(lines)
