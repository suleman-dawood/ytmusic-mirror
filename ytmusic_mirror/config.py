"""Configuration handling for ytmusic-mirror."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

CONFIG_FILE_NAME = ".playlist_config.json"
ARCHIVE_DIR_NAME = "_Archive"

DEFAULT_MUSIC_DIR = "~/Music/MP3s"


def _platform_config_dir(os_name: str, appdata: str, xdg: str, home: str) -> str:
    """Compute the OS config directory as a plain path string.

    Split out so the branch logic is unit-testable without needing a real
    Windows host (pathlib picks its flavour from os.name at runtime).
    """
    if os_name == "nt":
        base = appdata or home
    elif xdg:
        base = xdg
    else:
        base = os.path.join(home, ".config")
    return os.path.join(base, "ytmusic-mirror", "config.json")


def default_config_path() -> Path:
    """OS-aware location for the config file.

    Windows: %APPDATA%\\ytmusic-mirror\\config.json
    Linux/macOS: $XDG_CONFIG_HOME/ytmusic-mirror/config.json (or ~/.config)
    """
    return Path(
        _platform_config_dir(
            os.name,
            os.environ.get("APPDATA") or "",
            os.environ.get("XDG_CONFIG_HOME") or "",
            str(Path.home()),
        )
    )


DEFAULT_CONFIG_PATH = default_config_path()

DEFAULTS = {
    "music_dir": DEFAULT_MUSIC_DIR,
    "channel_url": "",
    "playlists": [],
    "cookies_from_browser": "",
    "cookie_file": "",
    "remote_components": [],
    "archive_dir": "",
    "deleted_playlist_policy": "archive",
    "orphan_policy": "smart",
    "download": {},
    "scheduler_enabled": False,
    "scheduler_cron": "0 0 * * *",
}


def expand_user_path(value: str) -> Path:
    """Expand env vars/~ and resolve to an absolute path.

    Raises ValueError when a value begins with '~' but is not a plain home
    reference (`~`, `~/...` or `~\\...`), which usually means the user typed
    `~Music/...` instead of `~/Music/...`. The check is done before
    os.path.expanduser so it behaves the same on Windows and Linux.
    """
    expanded = os.path.expandvars(value)
    if expanded.startswith("~"):
        rest = expanded[1:]
        if rest and not (rest.startswith("/") or rest.startswith(os.sep)):
            raise ValueError(
                f"Path '{value}' starts with '~' but is not a valid home path. "
                "Did you mean '~/' + the rest (e.g. '~/Music/MP3s')? Use an "
                "absolute path or '~/...'."
            )
        expanded = os.path.expanduser(expanded)
        if expanded.startswith("~"):
            raise ValueError(
                f"Path '{value}' could not be expanded to a home directory. "
                "Use an absolute path or '~/...'."
            )
    return Path(expanded).resolve()


def _expand_path(value: str) -> Path:
    return expand_user_path(value)


@dataclass
class Config:
    music_dir: Path
    channel_url: str = ""
    playlists: List[str] = field(default_factory=list)
    cookies_from_browser: str = ""
    cookie_file: str = ""
    remote_components: List[str] = field(default_factory=list)
    archive_dir: Optional[Path] = None
    deleted_playlist_policy: str = "archive"
    orphan_policy: str = "smart"
    download: dict = field(default_factory=dict)
    scheduler_enabled: bool = False
    scheduler_cron: str = "0 0 * * *"

    @property
    def effective_archive_dir(self) -> Path:
        if self.archive_dir:
            return _expand_path(str(self.archive_dir))
        return self.music_dir / ARCHIVE_DIR_NAME

    def to_dict(self) -> dict:
        return {
            "music_dir": str(self.music_dir),
            "channel_url": self.channel_url,
            "playlists": list(self.playlists),
            "cookies_from_browser": self.cookies_from_browser,
            "cookie_file": self.cookie_file,
            "remote_components": list(self.remote_components),
            "archive_dir": str(self.archive_dir) if self.archive_dir else "",
            "deleted_playlist_policy": self.deleted_playlist_policy,
            "orphan_policy": self.orphan_policy,
            "download": dict(self.download),
            "scheduler_enabled": bool(self.scheduler_enabled),
            "scheduler_cron": self.scheduler_cron,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        merged = {**DEFAULTS, **(data or {})}
        raw_archive = str(merged.get("archive_dir") or "")
        cfg = cls(
            music_dir=_expand_path(str(merged["music_dir"])),
            channel_url=str(merged.get("channel_url") or ""),
            playlists=[str(x) for x in merged.get("playlists") or []],
            cookies_from_browser=str(merged.get("cookies_from_browser") or ""),
            cookie_file=str(merged.get("cookie_file") or ""),
            remote_components=[str(x) for x in merged.get("remote_components") or []],
            archive_dir=_expand_path(raw_archive) if raw_archive else None,
            deleted_playlist_policy=str(merged.get("deleted_playlist_policy") or "archive"),
            orphan_policy=str(merged.get("orphan_policy") or "smart"),
            download=dict(merged.get("download") or {}),
            scheduler_enabled=bool(merged.get("scheduler_enabled", False)),
            scheduler_cron=str(merged.get("scheduler_cron") or "0 0 * * *"),
        )
        return cfg

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        config_path = (path or DEFAULT_CONFIG_PATH).expanduser()
        if not config_path.is_file():
            raise FileNotFoundError(
                f"No config file at {config_path}. Run `ytmusic-mirror init` first."
            )
        with open(config_path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    def save(self, path: Optional[Path] = None) -> Path:
        config_path = _expand_path(str(path or DEFAULT_CONFIG_PATH))
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)
            f.write("\n")
        return config_path


def write_default_config(path: Optional[Path] = None, music_dir: Optional[Path] = None) -> Path:
    """Create a fresh default config file and return the path written."""
    cfg = Config(
        music_dir=_expand_path(str(music_dir or DEFAULTS["music_dir"])),
    )
    return cfg.save(path)
