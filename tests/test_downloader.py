"""Unit tests for the native downloader module (no network / no downloads)."""

from __future__ import annotations

from pathlib import Path

import pytest
from mutagen.id3 import ID3, TALB, TIT2, TRCK, WOAR

from ytmusic_mirror import downloader


def make_config_file(path: Path, video_id: str, track: int, title: str) -> None:
    tags = ID3()
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TRCK(encoding=3, text=str(track)))
    tags.add(TALB(encoding=3, text="Playlist"))
    tags.add(WOAR(f"https://www.youtube.com/watch?v={video_id}"))
    tags.save(path, v2_version=3)


def write_song(folder: Path, track: int, video_id: str, title: str) -> Path:
    path = folder / f"{track}. {title}-{video_id}.mp3"
    path.write_bytes(b"")
    make_config_file(path, video_id, track, title)
    return path


def test_sanitize_name():
    assert downloader.sanitize_name('a/b:c*d?e"f<g>h|i') == "a_b_c_d_e_f_g_h_i"


def test_settings_from_config_defaults_and_old_style():
    # Old upstream-style config with nested include_metadata maps cleanly.
    raw = {
        "url": "https://x/playlist?list=PLx",
        "track_num_in_name": True,
        "include_metadata": {"cover": False, "lyrics": True},
    }
    settings = downloader.settings_from_config(raw)
    assert settings["url"] == "https://x/playlist?list=PLx"
    inc = settings["include_metadata"]
    assert inc["cover"] is False  # respected
    assert inc["track"] is True  # default
    assert inc["lyrics"] is True  # from raw
    # Bare dict without include_metadata still gets sane defaults.
    bare = downloader.settings_from_config({"url": "u"})
    assert bare["include_metadata"]["cover"] is True
    assert bare["audio_codec"] == "mp3"


def test_scan_playlist_folder_and_duplicates(tmp_path):
    write_song(tmp_path, 1, "aaa", "Alpha")
    write_song(tmp_path, 2, "bbb", "Beta")
    (tmp_path / "notes.txt").write_text("not audio")
    songs = downloader.scan_playlist_folder(tmp_path)
    assert set(songs) == {"aaa", "bbb"}
    assert songs["aaa"].track_num == 1
    assert songs["bbb"].file_name == "2. Beta-bbb.mp3"

    write_song(tmp_path, 3, "aaa", "AlphaDuplicate")
    with pytest.raises(RuntimeError):
        downloader.scan_playlist_folder(tmp_path)


def test_scan_ignores_untagged(tmp_path):
    (tmp_path / "1. no-tags-xxx.mp3").write_bytes(b"")
    assert downloader.scan_playlist_folder(tmp_path) == {}


def test_sync_playlist_reorders_existing(monkeypatch, tmp_path):
    write_song(tmp_path, 1, "aa", "Alpha")
    write_song(tmp_path, 2, "bb", "Beta")
    write_song(tmp_path, 3, "cc", "Gamma")
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: True)
    settings = downloader.settings_from_config({"url": "u", "track_num_in_name": True})

    entries = [
        {"id": "bb", "title": "Beta", "available": True},
        {"id": "aa", "title": "Alpha", "available": True},
        {"id": "cc", "title": "Gamma", "available": True},
    ]
    result = downloader.sync_playlist(tmp_path, "Playlist", entries, settings, log=lambda m: None)

    names = sorted(p.name for p in tmp_path.glob("*.mp3"))
    assert names == ["1. Beta-bb.mp3", "2. Alpha-aa.mp3", "3. Gamma-cc.mp3"]
    assert downloader.scan_playlist_folder(tmp_path)["aa"].track_num == 2
    assert result["added"] == 0


def test_sync_playlist_skips_unavailable_and_removes_temps(monkeypatch, tmp_path):
    write_song(tmp_path, 1, "aa", "Alpha")
    (tmp_path / ".ytmirror-zz.mp3").write_bytes(b"leftover")
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: True)
    settings = downloader.settings_from_config({"url": "u"})
    entries = [
        {"id": "aa", "title": "Alpha", "available": True},
        {"id": "ghost", "title": "Ghost", "available": False},  # no local copy
    ]
    downloader.sync_playlist(tmp_path, "Playlist", entries, settings, log=lambda m: None)
    assert not (tmp_path / ".ytmirror-zz.mp3").exists()
    names = [p.name for p in tmp_path.glob("*.mp3")]
    assert names == ["1. Alpha-aa.mp3"]
