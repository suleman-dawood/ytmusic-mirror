"""Live (network) tests. Not run by default.

These exercise the real yt-dlp download path end to end. Enable them with:

    YT_MIRROR_LIVE=1 pytest -m live

The tests download a tiny clip from a public playlist, so expect them to take
a little while and require ffmpeg + internet.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("YT_MIRROR_LIVE") != "1",
    reason="live tests disabled; set YT_MIRROR_LIVE=1 to run",
)

from ytmusic_mirror import downloader  # noqa: E402
from ytmusic_mirror.config import Config  # noqa: E402
from ytmusic_mirror.core import _remote_entries, sync  # noqa: E402

LIVE_PLAYLIST = os.environ.get(
    "YT_MIRROR_LIVE_PLAYLIST",
    "https://www.youtube.com/playlist?list=PLTIog-Fd-BJ4",  # single public video
)


def test_live_create_then_update_then_rename(tmp_path):
    """Real end-to-end: create -> no-op update -> folder rename re-tag."""
    cfg = Config(
        music_dir=tmp_path / "MP3s",
        playlists=[LIVE_PLAYLIST],
        remote_components=["ejs:github"],
    )
    first = sync(cfg)
    assert first.created
    folders = [p for p in (tmp_path / "MP3s").iterdir() if p.is_dir()]
    assert folders, "expected a playlist folder to be created"
    folder = folders[0]
    mp3s = list(folder.glob("*.mp3"))
    assert mp3s, "expected at least one downloaded mp3"
    song = downloader.scan_playlist_folder(folder)
    assert song, "downloaded file must carry WOAR identity tags"

    # Update run: same folder, nothing new.
    second = sync(cfg)
    assert second.updated

    # Simulate a rename (wrong local folder name) -> sync renames + retags.
    renamed = tmp_path / "MP3s" / "renamed-locally"
    folder.rename(renamed)
    third = sync(cfg)
    assert third.renamed
    back = tmp_path / "MP3s" / folder.name
    assert back.is_dir()


def test_live_downloader_single(tmp_path):
    cfg = Config(
        music_dir=tmp_path / "MP3s",
        playlists=[LIVE_PLAYLIST],
        remote_components=["ejs:github"],
    )
    settings = downloader.new_settings(cfg, LIVE_PLAYLIST)
    from ytmusic_mirror.core import _remote_entries

    entries = _remote_entries(cfg, LIVE_PLAYLIST)
    assert entries
    result = downloader.sync_playlist(
        tmp_path / "MP3s" / "Live", "Live", entries, settings, log=lambda m: None
    )
    assert result["added"] >= 1
    assert not result["failed"]
