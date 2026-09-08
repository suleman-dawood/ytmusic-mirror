"""Thin wrapper around the vendored youtube-music-downloader module.

Reuses the battle-tested download/metadata/ordering logic from
https://github.com/onnowhere/youtube_music_playlist_downloader (MIT, vendored).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import CONFIG_FILE_NAME

_MODULE_NAME = "ytmusic_mirror_downloader"
_loader_cache: Optional[Any] = None


def _load_downloader_module():
    """Import the vendored (or overridden) downloader module exactly once."""
    global _loader_cache
    if _loader_cache is not None:
        return _loader_cache

    candidates = []
    override = os.environ.get("YTMUSIC_MIRROR_DOWNLOADER")
    if override:
        candidates.append(Path(override))
    candidates.append(Path(__file__).parent / "vendor" / "youtube_music_playlist_downloader.py")

    for candidate in candidates:
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location(_MODULE_NAME, str(candidate))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _loader_cache = module
            return module

    raise RuntimeError(
        "Could not locate youtube_music_playlist_downloader.py. "
        "Set YTMUSIC_MIRROR_DOWNLOADER to the file path if not vendored."
    )


def get_module():
    return _load_downloader_module()


def setup_config(config: dict) -> dict:
    """Fill in all defaults the downloader expects and validate stored values."""
    return get_module().setup_config(config)


def get_playlist_info(config: dict) -> dict:
    """Fetch a single playlist's extract_flat info (title + ordered entries)."""
    return get_module().get_playlist_info(config)


def generate_playlist(
    config: dict,
    update: bool,
    current_playlist_name: Optional[str] = None,
    force_update: bool = False,
    regenerate_metadata: bool = False,
) -> None:
    """Download/sync one playlist into its folder.

    Must be called with the music directory as the current working directory.
    Creates the playlist folder when `update` is False, otherwise updates and
    renames an existing folder in place.
    """
    get_module().generate_playlist(
        config,
        CONFIG_FILE_NAME,
        update,
        force_update,
        regenerate_metadata,
        False,
        current_playlist_name,
        None,
    )


def get_local_song_files(playlist_folder: str) -> Dict[str, Any]:
    """Map video id -> SongFileInfo for every tagged song in a playlist folder."""
    return get_module().get_song_file_infos(playlist_folder)


def sanitize_folder_name(title: str) -> str:
    return get_module().format_file_name(title)
