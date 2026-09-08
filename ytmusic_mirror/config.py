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


def default_config_path() -> Path:
    """OS-aware location for the config file.

    Windows: %APPDATA%\\ytmusic-mirror\\config.json
    Linux/macOS: $XDG_CONFIG_HOME/ytmusic-mirror/config.json (or ~/.config)
    """
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "ytmusic-mirror" / "config.json"
        return Path.home() / "ytmusic-mirror" / "config.json"
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "ytmusic-mirror" / "config.json"
    return Path.home() / ".config" / "ytmusic-mirror" / "config.json"


DEFAULT_CONFIG_PATH = default_config_path()

DEFAULTS = {
    "music_dir": DEFAULT_MUSIC_DIR,
    "channel_url": "",
    "playlists": [],
    "cookies_from_browser": "",
    "cookie_file": "",
    "archive_dir": "",
    "deleted_playlist_policy": "archive",
    "orphan_policy": "smart",
    "download": {},
}


def _expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


@dataclass
class Config:
    music_dir: Path
    channel_url: str = ""
    playlists: List[str] = field(default_factory=list)
    cookies_from_browser: str = ""
    cookie_file: str = ""
    archive_dir: Optional[Path] = None
    deleted_playlist_policy: str = "archive"
    orphan_policy: str = "smart"
    download: dict = field(default_factory=dict)

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
            "archive_dir": str(self.archive_dir) if self.archive_dir else "",
            "deleted_playlist_policy": self.deleted_playlist_policy,
            "orphan_policy": self.orphan_policy,
            "download": dict(self.download),
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
            archive_dir=_expand_path(raw_archive) if raw_archive else None,
            deleted_playlist_policy=str(merged.get("deleted_playlist_policy") or "archive"),
            orphan_policy=str(merged.get("orphan_policy") or "smart"),
            download=dict(merged.get("download") or {}),
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
