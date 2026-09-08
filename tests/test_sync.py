"""Offline tests for ytmusic-mirror core logic (no network, no real downloads)."""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from ytmusic_mirror import core
from ytmusic_mirror.config import CONFIG_FILE_NAME, Config
from ytmusic_mirror.core import (
    LocalPlaylist,
    RemotePlaylist,
    SyncReport,
    _handle_deleted_playlist,
    _reconcile_orphans,
    discover_remote_playlists,
    playlist_id_from_url,
    pretty_report,
    snapshot_local_playlists,
    sync,
)

CFG = {
    "music_dir": "/tmp",
    "channel_url": "",
    "playlists": [],
    "cookies_from_browser": "",
    "cookie_file": "",
    "archive_dir": "",
    "deleted_playlist_policy": "archive",
    "orphan_policy": "smart",
    "download": {},
}


def make_config(root: Path, **overrides) -> Config:
    data = {**CFG, "music_dir": str(root)}
    data.update(overrides)
    return Config.from_dict(data)


def write_playlist_folder(root: Path, title: str, playlist_id: str) -> Path:
    folder = root / title
    folder.mkdir(parents=True, exist_ok=True)
    (folder / CONFIG_FILE_NAME).write_text(
        json.dumps({"url": f"https://www.youtube.com/playlist?list={playlist_id}"})
    )
    return folder


class FakeSongInfo:
    def __init__(self, video_id: str, file_name: str):
        self.video_id = video_id
        self.file_name = file_name
        self.name = file_name


def fake_local_files(song_ids):
    return {
        vid: FakeSongInfo(vid, f"{i + 1}. Song-{vid}.mp3")
        for i, vid in enumerate(song_ids)
    }


def test_playlist_id_from_url():
    assert playlist_id_from_url("https://www.youtube.com/playlist?list=PLabc&x=1") == "PLabc"
    assert playlist_id_from_url("https://x.com/watch?v=aaa") is None


def test_snapshot_local(tmp_path):
    write_playlist_folder(tmp_path, "One", "PLone")
    write_playlist_folder(tmp_path, "Two", "PLtwo")
    (tmp_path / "_Ignore").mkdir()
    (tmp_path / "_Ignore" / CONFIG_FILE_NAME).write_text("{}")

    local = snapshot_local_playlists(tmp_path)
    assert set(local.keys()) == {"PLone", "PLtwo"}
    assert local["PLone"].folder == "One"


def test_snapshot_local_duplicates_raise(tmp_path):
    write_playlist_folder(tmp_path, "One", "PLsame")
    write_playlist_folder(tmp_path, "Two", "PLsame")
    with pytest.raises(RuntimeError):
        snapshot_local_playlists(tmp_path)


def test_discovery_explicit_list(monkeypatch, tmp_path):
    cfg = make_config(
        tmp_path,
        channel_url="https://www.youtube.com/@someone",
        playlists=["https://www.youtube.com/playlist?list=PLx1"],
    )
    calls = {}

    def fake_enum(channel_url, cfg):
        return [RemotePlaylist("PLc", "Channel Playlist", "https://y/playlist?list=PLc")]

    def fake_title(url, cfg):
        calls["url"] = url
        return "Explicit Title"

    monkeypatch.setattr(core, "enumerate_channel_playlists", fake_enum)
    monkeypatch.setattr(core, "_fetch_playlist_title", fake_title)

    result = discover_remote_playlists(cfg)
    assert [r.id for r in result] == ["PLc", "PLx1"]
    assert result[1].title == "Explicit Title"
    assert calls["url"].startswith("https://")


def test_handle_deleted_archive(tmp_path):
    cfg = make_config(tmp_path)
    folder = write_playlist_folder(tmp_path, "Gone", "PLgone")
    report = SyncReport()
    plan = core.PlaylistPlan("PLgone", "Gone", "", "archive_deleted", folder="Gone")
    _handle_deleted_playlist(cfg, plan, dry_run=False, report=report, log=core.Logger(lambda m: None))
    assert not folder.exists()
    assert (cfg.effective_archive_dir / "deleted" / "PLgone").is_dir()


def test_handle_deleted_delete(tmp_path):
    cfg = make_config(tmp_path, deleted_playlist_policy="delete")
    folder = write_playlist_folder(tmp_path, "Gone", "PLgone")
    report = SyncReport()
    plan = core.PlaylistPlan("PLgone", "Gone", "", "archive_deleted", folder="Gone")
    _handle_deleted_playlist(cfg, plan, dry_run=False, report=report, log=core.Logger(lambda m: None))
    assert not folder.exists()
    assert not (cfg.effective_archive_dir / "deleted" / "PLgone").exists()


def test_reconcile_orphans_smart(monkeypatch, tmp_path):
    cfg = make_config(tmp_path)
    folder = write_playlist_folder(tmp_path, "Play", "PLp")
    still_up = "video_still_up"
    delisted = "video_delisted"
    files = fake_local_files([still_up, delisted])
    for info in files.values():
        (folder / info.file_name).write_bytes(b"")
    monkeypatch.setattr(core.engine, "get_local_song_files", lambda name: files)
    monkeypatch.setattr(core, "_probe_video_available", lambda vid, cfg: vid == still_up)

    report = SyncReport()
    plan = core.PlaylistPlan("PLp", "Play", "", "update", folder="Play")
    _reconcile_orphans(cfg, plan, [still_up, delisted], report, core.Logger(lambda m: None))

    assert report.removed_songs and any(still_up in s for s in report.removed_songs)
    assert report.delisted_songs and any(delisted in s for s in report.delisted_songs)
    assert not (folder / files[still_up].file_name).exists()
    archived = cfg.effective_archive_dir / "delisted" / "PLp" / files[delisted].file_name
    assert archived.exists()


def test_sync_creates_and_updates(monkeypatch, tmp_path):
    root = tmp_path / "mp3s"
    cfg = make_config(root)

    remote = [
        RemotePlaylist("PLnew", "New Playlist", "https://y/playlist?list=PLnew"),
        RemotePlaylist("PLkeep", "Old Title", "https://y/playlist?list=PLkeep"),
    ]
    write_playlist_folder(root, "Old Title", "PLkeep")

    calls = []
    orig = types.SimpleNamespace()

    def fake_get_playlist_info(config):
        pid = playlist_id_from_url(config["url"])
        return {"title": {"PLnew": "New Playlist", "PLkeep": "Old Title"}[pid], "entries": []}

    def fake_generate(config, update=False, current_playlist_name=None, force_update=False, regenerate_metadata=False):
        calls.append((playlist_id_from_url(config["url"]), update, current_playlist_name))

    monkeypatch.setattr(core, "discover_remote_playlists", lambda cfg: remote)
    monkeypatch.setattr(core.engine, "get_playlist_info", fake_get_playlist_info)
    monkeypatch.setattr(core.engine, "generate_playlist", fake_generate)
    monkeypatch.setattr(core, "_remote_entries", lambda config: [])
    monkeypatch.setattr(core.engine, "get_local_song_files", lambda name: {})

    report = sync(cfg)
    assert "PLnew" in [c[0] for c in calls]
    assert "PLkeep" in [c[0] for c in calls]
    gen = {c[0]: c for c in calls}
    assert gen["PLnew"][1] is False  # update=False for brand new playlist
    assert gen["PLkeep"][1] is True
    assert gen["PLkeep"][2] == "Old Title"
    assert not report.errors


def test_sync_archives_deleted_playlist(monkeypatch, tmp_path):
    root = tmp_path / "mp3s"
    cfg = make_config(root, channel_url="https://www.youtube.com/@someone")
    write_playlist_folder(root, "Disappeared", "PLgone")
    monkeypatch.setattr(core, "discover_remote_playlists", lambda cfg: [])
    report = sync(cfg)
    assert report.archived_playlists == ["Disappeared"]
    assert (cfg.effective_archive_dir / "deleted" / "PLgone").is_dir()


def test_pretty_report():
    report = SyncReport()
    report.created.append("A")
    report.created.append("B")
    report.removed_songs.append("A: vid1")
    text = pretty_report(report)
    assert "Created playlists (2):" in text
    assert "  - A" in text and "  - B" in text
    assert "vid1" in text


def test_logger_quiet(capsys):
    core.Logger().info("shown")
    core.Logger().warn("warned")
    core.Logger(quiet=True).info("hidden")
    out, err = capsys.readouterr()
    assert "shown" in out
    assert "warned" in err
    assert "hidden" not in out and "hidden" not in err


def test_clean_temp_files(tmp_path):
    (tmp_path / "song.mp3.part").write_bytes(b"x")
    (tmp_path / "song.mp3.ytdl").write_bytes(b"x")
    (tmp_path / "song.mp3").write_bytes(b"x")
    core._clean_temp_files(tmp_path)
    names = {p.name for p in tmp_path.iterdir()}
    assert names == {"song.mp3"}


def test_default_config_path_os(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    from ytmusic_mirror.config import default_config_path

    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg")
    assert str(default_config_path()).startswith("/tmp/xdg/ytmusic-mirror")


def test_note_unavailable_songs(tmp_path, monkeypatch):
    folder = tmp_path / "Play"
    folder.mkdir()
    plan = core.PlaylistPlan("PLp", "Play", "", "update", folder="Play")
    monkeypatch.setattr(core.engine, "get_local_song_files", lambda n: {})
    report = SyncReport()
    core._note_unavailable_songs(
        plan, folder, ["vidUnav"], {"vidUnav": "Ghost Song"}, {"vidUnav"}, report
    )
    assert report.unavailable and "Ghost Song" in report.unavailable[0]
    notes = core._read_unavailable_notes(folder)
    assert "vidUnav" in notes


def test_note_unavailable_pruned_when_downloaded(tmp_path, monkeypatch):
    folder = tmp_path / "Play"
    folder.mkdir()
    core._write_unavailable_notes(folder, {"vidNowHave": {"first_seen": "x"}})
    plan = core.PlaylistPlan("PLp", "Play", "", "update", folder="Play")
    monkeypatch.setattr(
        core.engine, "get_local_song_files", lambda n: {"vidNowHave": object()}
    )
    report = SyncReport()
    core._note_unavailable_songs(plan, folder, [], {}, {"vidNowHave"}, report)
    assert core._read_unavailable_notes(folder) == {}
