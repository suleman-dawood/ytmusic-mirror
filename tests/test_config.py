"""Config path/platform coverage."""

from __future__ import annotations

from pathlib import Path

import pytest

from ytmusic_mirror.config import Config, write_default_config


def test_windows_config_path_uses_appdata():
    from ytmusic_mirror.config import _platform_config_dir

    path = _platform_config_dir("nt", "/Roaming", "", "/home/u")
    assert path == "/Roaming/ytmusic-mirror/config.json"


def test_windows_config_path_falls_back_to_home():
    from ytmusic_mirror.config import _platform_config_dir

    path = _platform_config_dir("nt", "", "", "/home/u")
    assert path == "/home/u/ytmusic-mirror/config.json"


def test_linux_xdg_and_default():
    from ytmusic_mirror.config import _platform_config_dir

    assert _platform_config_dir("posix", "", "/xdg", "/home/u") == (
        "/xdg/ytmusic-mirror/config.json"
    )
    assert _platform_config_dir("posix", "", "", "/home/u") == (
        "/home/u/.config/ytmusic-mirror/config.json"
    )


def test_custom_archive_dir(tmp_path):
    cfg = Config(music_dir=tmp_path / "music", archive_dir=str(tmp_path / "kept"))
    assert cfg.effective_archive_dir == (tmp_path / "kept").resolve()
    cfg2 = Config(music_dir=tmp_path / "music")
    assert cfg2.effective_archive_dir == cfg2.music_dir / "_Archive"


def test_load_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        Config.load(tmp_path / "nope.json")


def test_save_creates_parents(tmp_path):
    target = tmp_path / "deep" / "nest" / "config.json"
    cfg = Config(music_dir=tmp_path / "music")
    assert cfg.save(target) == target
    assert target.is_file()


def test_roundtrip_all_fields(tmp_path):
    path = tmp_path / "config.json"
    data = {
        "music_dir": str(tmp_path / "Music"),
        "channel_url": "https://www.youtube.com/@u",
        "playlists": ["https://y/playlist?list=PLx"],
        "cookies_from_browser": "firefox",
        "remote_components": ["ejs:github"],
        "archive_dir": str(tmp_path / "arc"),
        "deleted_playlist_policy": "delete",
        "orphan_policy": "archive",
        "download": {"audio_codec": "opus"},
    }
    cfg = Config.from_dict(data)
    cfg.save(path)
    reloaded = Config.load(path)
    assert reloaded.channel_url == data["channel_url"]
    assert reloaded.playlists == data["playlists"]
    assert reloaded.remote_components == ["ejs:github"]
    assert reloaded.orphan_policy == "archive"


def test_write_default_config_expands_tilde(tmp_path):
    target = tmp_path / "cfg.json"
    write_default_config(target, music_dir=None)
    cfg = Config.load(target)
    assert cfg.music_dir == (Path.home() / "Music" / "MP3s").resolve()
