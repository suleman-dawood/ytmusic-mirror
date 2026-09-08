"""Additional offline tests for downloader.py (tagging, fetch, placements)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mutagen.id3 import APIC, ID3, TALB, TIT2, TRCK, WOAR

from ytmusic_mirror import downloader


def make_config_file(path: Path, video_id: str, track: int, title: str) -> None:
    tags = ID3()
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TRCK(encoding=3, text=str(track)))
    tags.add(TALB(encoding=3, text="Old"))
    tags.add(WOAR(f"https://www.youtube.com/watch?v={video_id}"))
    tags.save(path, v2_version=3)


def write_song(folder: Path, track: int, video_id: str, title: str) -> Path:
    path = folder / f"{track}. {title}-{video_id}.mp3"
    path.write_bytes(b"")
    make_config_file(path, video_id, track, title)
    return path


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL with a canned result."""

    def __init__(self, result, make_tmp=None):
        self.result = result
        self.make_tmp = make_tmp

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        if self.make_tmp is not None:
            self.make_tmp(url)
        return self.result


def test_ffmpeg_available_detects_missing(monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(subprocess, "check_output", boom)
    assert downloader.ffmpeg_available() is False


def test_settings_new_roundtrip_and_config_file(tmp_path):
    import types

    cfg = types.SimpleNamespace(
        download={"audio_codec": "mp3", "track_num_in_name": True},
        remote_components=["ejs:github"],
        cookies_from_browser="chromium",
        cookie_file="",
    )
    settings = downloader.new_settings(cfg, "https://x/playlist?list=PLx")
    assert settings["remote_components"] == ["ejs:github"]
    assert settings["cookies_from_browser"] == "chromium"

    folder = tmp_path / "P"
    folder.mkdir()
    downloader.write_playlist_config(folder, settings)
    written = json.loads((folder / ".playlist_config.json").read_text())
    assert written["version"] == 2
    assert written["url"] == "https://x/playlist?list=PLx"
    # Round-trips back through settings_from_config.
    reloaded = downloader.settings_from_config(written)
    assert reloaded["remote_components"] == ["ejs:github"]


def test_update_track_and_album_tags(tmp_path):
    path = write_song(tmp_path, 1, "aa", "Alpha")
    downloader.update_track_tag(path, 5)
    downloader.update_album_tag(path, "Renamed")
    tags = ID3(path)
    assert str(tags.get("TRCK")) == "5"
    assert str(tags.get("TALB")) == "Renamed"


def test_tag_new_file_full(monkeypatch, tmp_path):
    tiny_jpeg = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xdb\x00\x43\x00\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11"
        b"\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00"
        b"\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\xff"
        b"\xd9"
    )
    monkeypatch.setattr(
        downloader, "_fetch_thumbnail_jpeg", lambda url: tiny_jpeg
    )
    info = {
        "id": "aa11",
        "title": "Some Song",
        "uploader": "Some Uploader",
        "artist": "Some Artist",
        "upload_date": "20230115",
        "thumbnail": "http://img",
    }
    settings = downloader.settings_from_config({"url": "u"})
    path = tmp_path / "out.mp3"
    path.write_bytes(b"")
    downloader.tag_new_file(path, info, "Playlist Title", 7, settings)

    tags = ID3(path)
    assert str(tags.get("TIT2")) == "Some Song"
    assert str(tags.get("TPE1")) == "Some Uploader"  # use_uploader default True
    assert str(tags.get("TALB")) == "Playlist Title"
    assert str(tags.get("TRCK")) == "7"
    assert str(tags.get("TDRC")) == "2023-01-15"
    assert [str(x) for x in tags.getall("WOAR")] == [
        "https://www.youtube.com/watch?v=aa11"
    ]
    assert tags.getall("APIC")


def test_tag_new_file_title_artist_and_no_cover(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader, "_fetch_thumbnail_jpeg", lambda url: None)
    settings = downloader.settings_from_config(
        {"url": "u", "use_title": False, "use_uploader": False,
         "include_metadata": {"cover": False, "url": True}}
    )
    info = {"id": "bb22", "track": "Track Name", "title": "Video Title",
            "artist": "An Artist", "upload_date": None}
    path = tmp_path / "out.mp3"
    path.write_bytes(b"")
    downloader.tag_new_file(path, info, "P", 1, settings)
    tags = ID3(path)
    assert str(tags.get("TIT2")) == "Track Name"
    assert str(tags.get("TPE1")) == "An Artist"
    assert not tags.getall("APIC")


def test_fetch_playlist_entries(monkeypatch):
    entries_raw = [
        {"id": "PLitem1", "title": "Hello", "channel_id": "UCx"},
        {"id": "PLitem2", "title": "[Deleted video]", "channel_id": None},
        None,
        {"title": "no id"},
    ]
    result = {"title": "My List", "entries": entries_raw}
    monkeypatch.setattr(
        downloader.yt_dlp, "YoutubeDL", lambda opts: FakeYDL(result)
    )
    data = downloader.fetch_playlist("https://y/playlist?list=PLx",
                                     downloader.settings_from_config({"url": "u"}))
    assert data["title"] == "My List"
    assert len(data["entries"]) == 2
    assert data["entries"][1]["available"] is False


def test_sync_playlist_downloads_new(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: True)

    titles = {"xx": "New Song", "yy": "New Song 2"}

    def fake_download(url, folder, settings):
        video_id = url.split("v=")[1]
        (folder / f".ytmirror-{video_id}.mp3").write_bytes(b"")
        return {"id": video_id, "title": titles[video_id], "uploader": "U",
                "upload_date": "20230101", "artist": "A",
                "thumbnail": None}

    monkeypatch.setattr(downloader, "download_song", fake_download)
    settings = downloader.settings_from_config({"url": "u", "thread_count": 2})
    entries = [
        {"id": "xx", "title": "New Song", "available": True},
        {"id": "yy", "title": "New Song 2", "available": True},
    ]
    result = downloader.sync_playlist(tmp_path, "P", entries, settings,
                                      log=lambda m: None)
    assert result["added"] == 2
    assert sorted(p.name for p in tmp_path.glob("*.mp3")) == [
        "1. New Song-xx.mp3", "2. New Song 2-yy.mp3"
    ]
    assert downloader.scan_playlist_folder(tmp_path)["xx"].track_num == 1


def test_sync_playlist_new_failure_keeps_others(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: True)

    def fake_download(url, folder, settings):
        if "bad" in url:
            raise RuntimeError("boom")
        video_id = url.split("v=")[1]
        (folder / f".ytmirror-{video_id}.mp3").write_bytes(b"")
        return {"id": video_id, "title": "Good Song", "uploader": "U"}

    monkeypatch.setattr(downloader, "download_song", fake_download)
    settings = downloader.settings_from_config({"url": "u"})
    entries = [
        {"id": "bad", "title": "Bad", "available": True},
        {"id": "gg", "title": "Good Song", "available": True},
    ]
    result = downloader.sync_playlist(tmp_path, "P", entries, settings,
                                      log=lambda m: None)
    assert len(result["failed"]) == 1
    assert sorted(p.name for p in tmp_path.glob("*.mp3")) == ["1. Good Song-gg.mp3"]


def test_sync_playlist_no_ffmpeg_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: False)
    with pytest.raises(RuntimeError):
        downloader.sync_playlist(tmp_path, "P", [], downloader.settings_from_config({"url": "u"}),
                                 log=lambda m: None)


def test_download_song_success(monkeypatch, tmp_path):
    result = {"id": "aa11", "title": "T"}

    def fake_extract(url, download=False):
        (tmp_path / ".ytmirror-aa11.mp3").write_bytes(b"")
        return result

    fake = FakeYDL(result, make_tmp=fake_extract)
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", lambda opts: fake)
    info = downloader.download_song("https://youtube.com/watch?v=aa11", tmp_path,
                                    downloader.settings_from_config({"url": "u"}))
    assert info["id"] == "aa11"
    assert (tmp_path / ".ytmirror-aa11.mp3").exists()


def test_download_song_rejects_non_watch_url():
    with pytest.raises(ValueError):
        downloader.download_song("https://youtube.com/playlist?list=PLx", Path("."),
                                 downloader.settings_from_config({"url": "u"}))


def test_ffmpeg_available_present(monkeypatch):
    import subprocess

    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: b"ffmpeg x")
    assert downloader.ffmpeg_available() is True


def test_new_settings_cookie_file(tmp_path):
    import types

    cfg = types.SimpleNamespace(
        download={}, remote_components=[], cookies_from_browser="",
        cookie_file=str(tmp_path / "cookies.txt"),
    )
    settings = downloader.new_settings(cfg, "https://x/playlist?list=PLx")
    assert settings["cookie_file"] == str(tmp_path / "cookies.txt")


def test_scan_ignores_multiple_woar_and_bad_track(tmp_path):
    from mutagen.id3 import ID3, TIT2, TRCK, WOAR

    tags = ID3()
    tags.add(TIT2(encoding=3, text="Dup"))
    tags.add(WOAR("https://www.youtube.com/watch?v=aa"))
    tags.add(WOAR("https://www.youtube.com/watch?v=bb"))
    path = tmp_path / "1. Dup-aa.mp3"
    path.write_bytes(b"")
    tags.save(path, v2_version=3)
    assert downloader.scan_playlist_folder(tmp_path) == {}

    tags2 = ID3()
    tags2.add(TIT2(encoding=3, text="Bad"))
    tags2.add(TRCK(encoding=3, text="not-a-number"))
    tags2.add(WOAR("https://www.youtube.com/watch?v=cc"))
    path2 = tmp_path / "2. Bad-cc.mp3"
    path2.write_bytes(b"")
    tags2.save(path2, v2_version=3)
    assert downloader.scan_playlist_folder(tmp_path)["cc"].track_num == 0


def test_scan_missing_folder_returns_empty(tmp_path):
    assert downloader.scan_playlist_folder(tmp_path / "nope") == {}


def test_download_song_missing_output_raises(monkeypatch, tmp_path):
    fake = FakeYDL({"id": "aa"}, make_tmp=None)
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", lambda opts: fake)
    with pytest.raises(RuntimeError):
        downloader.download_song("https://youtube.com/watch?v=aa", tmp_path,
                                 downloader.settings_from_config({"url": "u"}))


def test_fetch_thumbnail_jpeg(monkeypatch):
    import base64
    from io import BytesIO

    jpeg = base64.b64decode(
        "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
        "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"
        "AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AVN//2Q=="
    )

    monkeypatch.setattr(
        downloader.requests, "get",
        lambda url, **kw: type("Resp", (), {"raw": BytesIO(jpeg)})(),
    )
    data = downloader._fetch_thumbnail_jpeg("http://img")
    assert data is not None and data[:2] == b"\xff\xd8"

    def boom(*a, **k):
        raise OSError("net down")

    monkeypatch.setattr(downloader.requests, "get", boom)
    assert downloader._fetch_thumbnail_jpeg("http://img") is None


def test_tag_new_file_album_from_video_and_bad_date(tmp_path):
    settings = downloader.settings_from_config(
        {"url": "u", "use_playlist_name": False, "include_metadata": {"cover": False}}
    )
    info = {"id": "c", "title": "T", "artist": "A", "album": "Video Album",
            "upload_date": "not-a-date"}
    path = tmp_path / "out.mp3"
    path.write_bytes(b"")
    downloader.tag_new_file(path, info, "Playlist", 1, settings)
    tags = ID3(path)
    assert str(tags.get("TALB")) == "Video Album"
    assert tags.get("TDRC") is None


def test_sync_playlist_no_track_prefix(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: True)

    def fake_download(url, folder, settings):
        video_id = url.split("v=")[1]
        (folder / f".ytmirror-{video_id}.mp3").write_bytes(b"")
        return {"id": video_id, "title": "Plain", "uploader": "U", "thumbnail": None}

    monkeypatch.setattr(downloader, "download_song", fake_download)
    settings = downloader.settings_from_config(
        {"url": "u", "track_num_in_name": False}
    )
    entries = [{"id": "pp", "title": "Plain", "available": True}]
    downloader.sync_playlist(tmp_path, "P", entries, settings, log=lambda m: None)
    assert (tmp_path / "Plain-pp.mp3").exists()


def test_sync_playlist_download_claims_ok_but_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: True)

    def fake_download(url, folder, settings):
        return {"id": url.split("v=")[1], "title": "Ghost", "uploader": "U"}

    monkeypatch.setattr(downloader, "download_song", fake_download)
    settings = downloader.settings_from_config({"url": "u"})
    entries = [{"id": "gh", "title": "Ghost", "available": True}]
    result = downloader.sync_playlist(tmp_path, "P", entries, settings,
                                      log=lambda m: None)
    # temp missing -> no file placed, no crash
    assert list(tmp_path.glob("*.mp3")) == []


def test_sync_playlist_thread_failure_recorded(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: True)

    def fake_download(url, folder, settings):
        if "zz" in url:
            raise RuntimeError("threaded boom")
        video_id = url.split("v=")[1]
        (folder / f".ytmirror-{video_id}.mp3").write_bytes(b"")
        return {"id": video_id, "title": "Good", "uploader": "U"}

    monkeypatch.setattr(downloader, "download_song", fake_download)
    settings = downloader.settings_from_config({"url": "u", "thread_count": 2})
    entries = [
        {"id": "zz", "title": "Bad", "available": True},
        {"id": "gg", "title": "Good", "available": True},
    ]
    result = downloader.sync_playlist(tmp_path, "P", entries, settings,
                                      log=lambda m: None)
    assert len(result["failed"]) == 1
    assert "threaded boom" in result["failed"][0]
    assert [p.name for p in tmp_path.glob("*.mp3")] == ["1. Good-gg.mp3"]


def test_sync_playlist_leftover_unlink_error_swallowed(monkeypatch, tmp_path):
    from pathlib import Path as P

    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: True)
    leftover = tmp_path / ".ytmirror-zz.mp3"
    leftover.write_bytes(b"")

    def raising_unlink(self, *a, **k):
        raise OSError("locked")

    monkeypatch.setattr(P, "unlink", raising_unlink)
    downloader.sync_playlist(tmp_path, "P", [], downloader.settings_from_config({"url": "u"}),
                             log=lambda m: None)
    assert leftover.exists()  # removal failed but was swallowed


def test_fetch_thumbnail_inner_exception(monkeypatch):
    def bad_image_open(fp, **kw):
        class FakeImg:
            def convert(self, mode):
                raise ValueError("cannot convert")

        return FakeImg()

    monkeypatch.setattr(downloader.Image, "open", bad_image_open)
    assert downloader._fetch_thumbnail_jpeg("http://img") is None


def test_fetch_playlist_marker_title_unavailable(monkeypatch):
    result = {"title": "L", "entries": [
        {"id": "m", "title": "[Private video]", "channel_id": "UCx"},
    ]}
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", lambda opts: FakeYDL(result))
    data = downloader.fetch_playlist("https://y/playlist?list=PLx",
                                     downloader.settings_from_config({"url": "u"}))
    assert data["entries"][0]["available"] is False


def test_fetch_thumbnail_inner_exception_with_request_stub(monkeypatch):
    from types import SimpleNamespace

    def fake_get(url, **kw):
        return SimpleNamespace(raw=SimpleNamespace())

    def bad_image_open(fp, **kw):
        class FakeImg:
            def convert(self, mode):
                raise ValueError("cannot convert")

        return FakeImg()

    monkeypatch.setattr(downloader.requests, "get", fake_get)
    monkeypatch.setattr(downloader.Image, "open", bad_image_open)
    assert downloader._fetch_thumbnail_jpeg("http://img") is None


def test_tag_new_file_bad_date_type(tmp_path):
    settings = downloader.settings_from_config(
        {"url": "u", "include_metadata": {"cover": False, "date": True}}
    )
    info = {"id": "d", "title": "T", "artist": "A", "upload_date": 20230115}
    path = tmp_path / "out.mp3"
    path.write_bytes(b"")
    downloader.tag_new_file(path, info, "P", 1, settings)  # no crash
    assert ID3(path).get("TDRC") is None


def test_sync_reorder_tag_error(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: True)
    write_song(tmp_path, 1, "aa", "Alpha")
    write_song(tmp_path, 2, "bb", "Beta")

    def bad_update(path, track):
        raise RuntimeError("tag write failed")

    monkeypatch.setattr(downloader, "update_track_tag", bad_update)
    settings = downloader.settings_from_config({"url": "u", "track_num_in_name": True})
    entries = [
        {"id": "aa", "title": "Alpha", "available": True},
        {"id": "bb", "title": "Beta", "available": True},
    ]
    result = downloader.sync_playlist(tmp_path, "P", entries, settings,
                                      log=lambda m: None)
    assert any("Could not reorder" in f for f in result["failed"])
