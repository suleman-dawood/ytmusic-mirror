"""Offline coverage for core.py: discovery helpers, snapshot edge cases and
full sync flows exercised with mocked network/download layers."""

from __future__ import annotations

import json
from pathlib import Path

from mutagen.id3 import ID3, TALB, TIT2, TRCK, WOAR

from ytmusic_mirror import core
from ytmusic_mirror.config import CONFIG_FILE_NAME, Config
from ytmusic_mirror.core import (
    RemotePlaylist,
    SyncReport,
    discover_remote_playlists,
    playlist_id_from_url,
    snapshot_local_playlists,
    sync,
)


def make_config(root: Path, **overrides) -> Config:
    data = {
        "music_dir": str(root),
        "channel_url": "",
        "playlists": [],
        "cookies_from_browser": "",
        "cookie_file": "",
        "archive_dir": "",
        "deleted_playlist_policy": "archive",
        "orphan_policy": "smart",
        "download": {},
    }
    data.update(overrides)
    return Config.from_dict(data)


def tag_file(path: Path, video_id: str, track: int, title: str, album: str = "P") -> None:
    path.write_bytes(b"")
    tags = ID3()
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TRCK(encoding=3, text=str(track)))
    tags.add(TALB(encoding=3, text=album))
    tags.add(WOAR(f"https://www.youtube.com/watch?v={video_id}"))
    tags.save(path, v2_version=3)


def write_folder(root: Path, folder: str, playlist_id: str) -> Path:
    path = root / folder
    path.mkdir(parents=True, exist_ok=True)
    (path / CONFIG_FILE_NAME).write_text(
        json.dumps({"url": f"https://www.youtube.com/playlist?list={playlist_id}"})
    )
    return path


class FakeYDL:
    def __init__(self, info):
        self.info = info

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        return self.info


def entry(video_id: str, title: str, available: bool = True) -> dict:
    return {"id": video_id, "title": title, "available": available}


# --------------------------------------------------------------------------- #
# Discovery / URL helpers
# --------------------------------------------------------------------------- #


def test_channel_tab_url_variants():
    assert core._channel_playlists_tab_url("https://youtube.com/@u") == (
        "https://youtube.com/@u/playlists"
    )
    assert core._channel_playlists_tab_url("https://youtube.com/@u/playlists") == (
        "https://youtube.com/@u/playlists"
    )


def test_url_and_id_helpers():
    assert core.playlist_url_from_id("PLx") == "https://www.youtube.com/playlist?list=PLx"
    assert core._looks_like_playlist_id("PLabc") is True
    assert core._looks_like_playlist_id("abcdefghijk") is False
    assert core._looks_like_playlist_id("OLAK5uy_xyz") is True


def test_ytdl_base_and_flat_opts(tmp_path):
    cfg = make_config(tmp_path)
    opts = core._ytdl_base_opts(cfg)
    assert opts["cookiefile"] is None and opts["cookiesfrombrowser"] is None
    flat = core._flat_opts(cfg)
    assert flat["extract_flat"] is True

    cfg2 = make_config(tmp_path, cookie_file=str(tmp_path / "c.txt"),
                       cookies_from_browser="chromium")
    opts2 = core._ytdl_base_opts(cfg2)
    assert opts2["cookiefile"] is not None
    assert opts2["cookiesfrombrowser"] == ("chromium",)


def test_enumerate_channel_playlists(monkeypatch, tmp_path):
    cfg = make_config(tmp_path)
    info = {
        "entries": [
            {"id": "PLok", "title": "Good", "ie_key": "YoutubeTab"},
            {"id": "abcdefghijk", "title": "a video, filtered"},
            None,
            {"title": "no id"},
            {"id": "PLdup", "title": "Dup"},
        ]
    }
    monkeypatch.setattr(core.yt_dlp, "YoutubeDL", lambda opts: FakeYDL(info))
    found = core.enumerate_channel_playlists("https://www.youtube.com/@u", cfg)
    assert [f.id for f in found] == ["PLok", "PLdup"]


def test_discover_dedupes_and_validates(monkeypatch, tmp_path):
    cfg = make_config(
        tmp_path,
        channel_url="https://www.youtube.com/@u",
        playlists=[
            "https://youtube.com/playlist?list=PLsame",  # duplicate of channel
            "",  # skipped
            "https://youtube.com/playlist?list=PLexplicit",
        ],
    )

    def fake_enum(channel_url, cfg):
        return [RemotePlaylist("PLsame", "Same", "u")]

    monkeypatch.setattr(core, "enumerate_channel_playlists", fake_enum)
    monkeypatch.setattr(core, "_fetch_playlist_title", lambda url, cfg: "Explicit")
    result = discover_remote_playlists(cfg)
    assert [r.id for r in result] == ["PLsame", "PLexplicit"]

    bad = make_config(tmp_path, channel_url="", playlists=["https://y.com/watch?v=x"])
    with __import__("pytest").raises(ValueError):
        discover_remote_playlists(bad)


def test_fetch_playlist_title_fallback(monkeypatch, tmp_path):
    cfg = make_config(tmp_path)

    def ok(url, settings):
        return {"title": "Remote Name", "entries": []}

    monkeypatch.setattr(core.downloader, "fetch_playlist", ok)
    assert core._fetch_playlist_title("https://y/playlist?list=PLa", cfg) == "Remote Name"

    def boom(url, settings):
        raise RuntimeError("net")

    monkeypatch.setattr(core.downloader, "fetch_playlist", boom)
    assert core._fetch_playlist_title("https://y/playlist?list=PLa", cfg) == (
        "https://y/playlist?list=PLa"
    )


def test_remote_entries_exception_returns_empty(monkeypatch, tmp_path):
    cfg = make_config(tmp_path)

    def boom(url, settings):
        raise RuntimeError("net down")

    monkeypatch.setattr(core.downloader, "fetch_playlist", boom)
    assert core._remote_entries(cfg, "https://y/playlist?list=PLa") == []


# --------------------------------------------------------------------------- #
# Sidecar + snapshot edge cases
# --------------------------------------------------------------------------- #


def test_read_unavailable_bad_json(tmp_path):
    (tmp_path / core.SIDECAR_NAME).write_text("{not json")
    assert core._read_unavailable_notes(tmp_path) == {}


def test_snapshot_missing_dir_and_invalid(tmp_path):
    assert snapshot_local_playlists(tmp_path / "missing") == {}

    # A plain file and a folder without a config are both skipped.
    (tmp_path / "random.mp3").write_bytes(b"")
    (tmp_path / "no-config").mkdir()
    write_folder(tmp_path, "Real", "PLreal")
    assert set(snapshot_local_playlists(tmp_path)) == {"PLreal"}

    # Invalid JSON config -> RuntimeError.
    folder = write_folder(tmp_path / "sub_a", "Bad", "PLbad")
    (folder / CONFIG_FILE_NAME).write_text("{not json")
    try:
        snapshot_local_playlists(tmp_path / "sub_a")
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass

    # Non-playlist URL in config -> RuntimeError.
    folder2 = write_folder(tmp_path / "sub_b", "Bad2", "PLb2")
    (folder2 / CONFIG_FILE_NAME).write_text(
        json.dumps({"url": "https://y.com/watch?v=x"})
    )
    try:
        snapshot_local_playlists(tmp_path / "sub_b")
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass


def test_merge_cookies_and_remote_components(tmp_path):
    cfg = make_config(tmp_path, cookies_from_browser="firefox",
                      remote_components=["ejs:github"])
    config = {"cookies_from_browser": "", "remote_components": []}
    core._merge_cookies(cfg, config)
    assert config["cookies_from_browser"] == "firefox"
    assert config["remote_components"] == ["ejs:github"]
    # already-set values are kept
    core._merge_cookies(cfg, config)
    assert config["cookies_from_browser"] == "firefox"


# --------------------------------------------------------------------------- #
# probe helper
# --------------------------------------------------------------------------- #


def test_probe_video_available(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)

    def probe_returning(info):
        def fake_ydl(opts):
            return FakeYDL(info)

        return fake_ydl

    monkeypatch.setattr(core.yt_dlp, "YoutubeDL",
                        probe_returning({"channel_id": "UCx", "title": "Fine"}))
    assert core._probe_video_available("aa", cfg) is True

    monkeypatch.setattr(core.yt_dlp, "YoutubeDL",
                        probe_returning({"channel_id": None, "title": "Whatever"}))
    assert core._probe_video_available("aa", cfg) is False

    monkeypatch.setattr(core.yt_dlp, "YoutubeDL",
                        probe_returning({"channel_id": "UCx", "title": "[Private video]"}))
    assert core._probe_video_available("aa", cfg) is False

    monkeypatch.setattr(core.yt_dlp, "YoutubeDL", probe_returning({}))
    assert core._probe_video_available("aa", cfg) is False

    def raise_ydl(opts):
        raise RuntimeError("net")

    monkeypatch.setattr(core.yt_dlp, "YoutubeDL", raise_ydl)
    assert core._probe_video_available("aa", cfg) is False


# --------------------------------------------------------------------------- #
# Full sync flows (offline)
# --------------------------------------------------------------------------- #


def test_sync_misconfigured_reports_error(tmp_path):
    cfg = make_config(tmp_path)  # no channel, no playlists
    report = sync(cfg)
    assert report.errors


def test_sync_dry_run_and_full_create_update_rename(monkeypatch, tmp_path):
    root = tmp_path / "mp3s"
    cfg = make_config(root)
    remote = [
        RemotePlaylist("PLa", "New Playlist", "https://y/playlist?list=PLa"),
        RemotePlaylist("PLb", "Renamed B", "https://y/playlist?list=PLb"),
    ]
    # Existing folder for PLb under the OLD title, with two tagged songs.
    folder_b = write_folder(root, "Old B", "PLb")
    tag_file(folder_b / "1. Alpha-aa.mp3", "aa", 1, "Alpha", "Old B")
    tag_file(folder_b / "2. Beta-bb.mp3", "bb", 2, "Beta", "Old B")

    calls = []

    def fake_sync(folder, playlist_title, entries, settings, log=None):
        calls.append((Path(folder).name, playlist_title, entries))
        return {"added": 0, "failed": [], "renamed": 0}

    def fake_entries(cfg, url):
        pid = playlist_id_from_url(url)
        if pid == "PLa":
            return []
        return [entry("aa", "Alpha"), entry("bb", "Beta")]

    monkeypatch.setattr(core, "discover_remote_playlists", lambda cfg: remote)
    monkeypatch.setattr(core.downloader, "sync_playlist", fake_sync)
    monkeypatch.setattr(core, "_remote_entries", fake_entries)

    # --- dry run first: nothing should change on disk ---
    dry = sync(cfg, dry_run=True)
    assert dry.created == ["New Playlist"]
    assert "Renamed B" in dry.updated
    assert not (root / "New Playlist").exists()
    assert (root / "Old B").exists()

    # --- real run ---
    report = sync(cfg)
    assert (root / "New Playlist").is_dir()
    assert (root / "New Playlist" / CONFIG_FILE_NAME).is_file()
    assert not (root / "Old B").exists()
    assert (root / "Renamed B").exists()
    # Album tags follow the renamed playlist.
    tags = ID3(root / "Renamed B" / "1. Alpha-aa.mp3")
    assert str(tags.get("TALB")) == "Renamed B"
    assert not report.errors
    assert [c[0] for c in calls] == ["New Playlist", "Renamed B"]


def test_sync_orphan_delete_and_archive(monkeypatch, tmp_path):
    root = tmp_path / "mp3s"
    cfg = make_config(root, channel_url="https://www.youtube.com/@u")
    folder = write_folder(root, "Keep", "PLk")
    tag_file(folder / "1. Alpha-aa.mp3", "aa", 1, "Alpha")
    tag_file(folder / "2. StillUp-ss.mp3", "ss", 2, "Still Up")
    tag_file(folder / "3. Delisted-dd.mp3", "dd", 3, "Delisted")

    probes = {"ss": True, "dd": False}

    def fake_sync(folder, playlist_title, entries, settings, log=None):
        return {"added": 0, "failed": [], "renamed": 0}

    monkeypatch.setattr(core, "discover_remote_playlists",
                        lambda cfg: [RemotePlaylist("PLk", "Keep", "https://y/playlist?list=PLk")])
    monkeypatch.setattr(core, "_remote_entries", lambda cfg, url: [entry("aa", "Alpha")])
    monkeypatch.setattr(core, "_probe_video_available", lambda vid, cfg: probes[vid])
    monkeypatch.setattr(core.downloader, "sync_playlist", fake_sync)

    report = sync(cfg)
    assert any("ss" in r for r in report.removed_songs)          # user removed, deleted
    assert not (folder / "2. StillUp-ss.mp3").exists()
    assert any("dd" in r for r in report.delisted_songs)         # delisted, archived
    assert not (folder / "3. Delisted-dd.mp3").exists()
    assert (cfg.effective_archive_dir / "delisted" / "PLk" / "3. Delisted-dd.mp3").exists()
    assert (folder / "1. Alpha-aa.mp3").exists()


def test_sync_deleted_playlist_keep(monkeypatch, tmp_path):
    root = tmp_path / "mp3s"
    cfg = make_config(root, channel_url="https://www.youtube.com/@u",
                      deleted_playlist_policy="keep")
    write_folder(root, "Ghost", "PLghost")
    monkeypatch.setattr(core, "discover_remote_playlists", lambda cfg: [])
    report = sync(cfg)
    assert report.archived_playlists == []
    assert (root / "Ghost").exists()


def test_sync_deleted_playlist_archive_dry_run(monkeypatch, tmp_path):
    root = tmp_path / "mp3s"
    cfg = make_config(root, channel_url="https://www.youtube.com/@u")
    write_folder(root, "Ghost", "PLghost")
    monkeypatch.setattr(core, "discover_remote_playlists", lambda cfg: [])
    dry = sync(cfg, dry_run=True)
    assert dry.archived_playlists == ["Ghost"]
    assert (root / "Ghost").exists()


def test_enumerate_no_scheme_and_duplicate_channel(monkeypatch, tmp_path):
    cfg = make_config(tmp_path)
    info = {"entries": [
        {"id": "PLa", "title": "A"},
        {"id": "PLa", "title": "A again"},
    ]}
    monkeypatch.setattr(core.yt_dlp, "YoutubeDL", lambda opts: FakeYDL(info))
    # Bare host (no scheme) exercises the https:// prefix branch.
    found = core.enumerate_channel_playlists("youtube.com/@u", cfg)
    assert found[0].url.startswith("https://")
    assert len(found) == 2  # enumeration keeps both; dedupe happens downstream


def test_remote_entries_success(monkeypatch, tmp_path):
    cfg = make_config(tmp_path)

    def ok(url, settings):
        return {"title": "T", "entries": [
            {"id": "aa", "title": "A", "available": True},
            {"id": "bb", "title": "B", "available": False},
        ]}

    monkeypatch.setattr(core.downloader, "fetch_playlist", ok)
    entries = core._remote_entries(cfg, "https://y/playlist?list=PLa")
    assert [e["id"] for e in entries] == ["aa", "bb"]
    assert entries[1]["available"] is False


def test_dry_run_reports_unavailable(monkeypatch, tmp_path):
    root = tmp_path / "mp3s"
    cfg = make_config(root)
    remote = [RemotePlaylist("PLu", "Unavailable List",
                             "https://y/playlist?list=PLu")]
    monkeypatch.setattr(core, "discover_remote_playlists", lambda cfg: remote)
    monkeypatch.setattr(
        core, "_remote_entries", lambda cfg, url: [
            entry("ghost", "Gone", available=False)
        ]
    )
    dry = sync(cfg, dry_run=True)
    assert dry.created == ["Unavailable List"]
    assert "PLu" not in str(dry.updated)


def test_sync_warnings_and_sync_failure(monkeypatch, tmp_path):
    root = tmp_path / "mp3s"
    cfg = make_config(root)
    remote = [RemotePlaylist("PLf", "Fails", "https://y/playlist?list=PLf")]
    state = {"raise": False}

    def fake_sync(folder, playlist_title, entries, settings, log=None):
        if state["raise"]:
            raise RuntimeError("downloader exploded")
        return {"added": 0, "failed": ["https://y/watch?v=x: net"], "renamed": 0}

    monkeypatch.setattr(core, "discover_remote_playlists", lambda cfg: remote)
    monkeypatch.setattr(core, "_remote_entries", lambda cfg, url: [])
    monkeypatch.setattr(core.downloader, "sync_playlist", fake_sync)

    report = sync(cfg)
    assert any("net" in w for w in report.warnings)
    assert not report.errors

    state["raise"] = True
    report = sync(cfg)
    assert any("downloader exploded" in e for e in report.errors)


def test_note_write_oserror(monkeypatch, tmp_path):
    folder = tmp_path / "Play"
    folder.mkdir()
    # Make the sidecar path un-writable by pre-creating it as a directory.
    (folder / core.SIDECAR_NAME).mkdir()
    plan = core.PlaylistPlan("PLp", "Play", "", "update", folder="Play")
    report = SyncReport()
    monkeypatch.setattr(core.downloader, "scan_playlist_folder", lambda f: {})
    core._note_unavailable_songs(plan, folder, ["x"], {"x": ""}, {"x"}, report)
    assert report.errors


def test_handle_deleted_missing_folder_and_delete_dry(tmp_path):
    cfg = make_config(tmp_path, deleted_playlist_policy="delete")
    report = SyncReport()
    plan = core.PlaylistPlan("PLx", "Gone", "", "archive_deleted", folder="Nope")
    core._handle_deleted_playlist(cfg, plan, dry_run=False, report=report,
                                  log=core.Logger(lambda m: None))
    assert not report.deleted_playlists  # missing folder -> early return

    folder = write_folder(tmp_path, "Gone", "PLx")
    plan_dry = core.PlaylistPlan("PLx", "Gone", "", "archive_deleted", folder="Gone")
    core._handle_deleted_playlist(cfg, plan_dry, dry_run=True, report=report,
                                  log=core.Logger(lambda m: None))
    assert report.deleted_playlists == ["Gone"]
    assert folder.exists()  # dry run leaves it


def test_classify_orphans_dry_variants(tmp_path):
    cfg_archive = make_config(tmp_path, orphan_policy="archive")
    cfg_delete = make_config(tmp_path, orphan_policy="delete")
    plan = core.PlaylistPlan("PLp", "Play", "", "update", folder="Play")
    report = SyncReport()

    core._classify_orphans_dry(cfg_archive, plan, ["a"], report)
    assert report.delisted_songs and "a" in report.delisted_songs[0]

    core._classify_orphans_dry(cfg_delete, plan, ["b"], report)
    assert any("b" in r for r in report.removed_songs)


def test_reconcile_orphans_missing_file_skipped(monkeypatch, tmp_path):
    folder = write_folder(tmp_path, "Play", "PLp")
    cfg = make_config(tmp_path)
    report = SyncReport()
    plan = core.PlaylistPlan("PLp", "Play", "", "update", folder="Play")
    # scan returns {} even though we ask for orphan "x" -> skip silently.
    monkeypatch.setattr(core.downloader, "scan_playlist_folder", lambda f: {})
    core._reconcile_orphans(cfg, plan, ["x"], report, core.Logger(lambda m: None))
    assert not report.errors


def test_pretty_report_all_sections():
    report = SyncReport()
    report.created.append("C")
    report.updated.append("U")
    report.renamed.append("A -> B")
    report.archived_playlists.append("Arch")
    report.deleted_playlists.append("Del")
    report.removed_songs.append("P: vid")
    report.delisted_songs.append("P: vid2")
    report.unavailable.append("P: ghost")
    report.skipped_playlists.append("Skip")
    report.warnings.append("w1")
    report.errors.append("e1")
    text = core.pretty_report(report)
    for needle in (
        "Created playlists (1)", "Updated playlists (1)", "Renamed playlists (1)",
        "Archived playlists (1)", "Deleted playlists (1)", "delisted", "unavailable",
        "Skipped playlists", "Warnings", "Errors",
    ):
        assert needle in text


def test_discover_channel_duplicates_skipped(monkeypatch, tmp_path):
    cfg = make_config(tmp_path, channel_url="https://www.youtube.com/@u",
                      playlists=["https://y/playlist?list=PLx"])
    monkeypatch.setattr(core, "enumerate_channel_playlists",
                        lambda u, cfg: [
                            RemotePlaylist("PLsame", "One", "u"),
                            RemotePlaylist("PLsame", "One again", "u"),
                        ])
    monkeypatch.setattr(core, "_fetch_playlist_title", lambda url, cfg: "X")
    result = discover_remote_playlists(cfg)
    assert [r.id for r in result] == ["PLsame", "PLx"]


def test_sync_discovery_error(monkeypatch, tmp_path):
    cfg = make_config(tmp_path, channel_url="https://www.youtube.com/@u")

    def boom(cfg):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(core, "discover_remote_playlists", boom)
    report = sync(cfg)
    assert any("network exploded" in e for e in report.errors)


def test_merge_cookies_sets_cookie_file(tmp_path):
    cfg = make_config(tmp_path, cookie_file="/tmp/cookies.txt")
    config = {"cookies_from_browser": "", "cookie_file": "", "remote_components": []}
    core._merge_cookies(cfg, config)
    assert config["cookie_file"] == "/tmp/cookies.txt"


def test_classify_orphans_dry_smart(monkeypatch, tmp_path):
    cfg = make_config(tmp_path, orphan_policy="smart")
    plan = core.PlaylistPlan("PLp", "Play", "", "update", folder="Play")
    report = SyncReport()
    monkeypatch.setattr(core, "_probe_video_available",
                        lambda vid, cfg: vid == "up")
    core._classify_orphans_dry(cfg, plan, ["up", "down"], report)
    assert any("up" in r for r in report.removed_songs)
    assert any("down" in r for r in report.delisted_songs)


def test_note_unavailable_folder_missing_and_scan_error(monkeypatch, tmp_path):
    plan = core.PlaylistPlan("PLp", "Play", "", "update", folder="Play")
    report = SyncReport()
    # Folder does not exist -> return early, no crash.
    core._note_unavailable_songs(plan, tmp_path / "missing", ["x"], {"x": ""}, {"x"}, report)
    assert not report.errors

    folder = tmp_path / "Play"
    folder.mkdir()
    core._write_unavailable_notes(folder, {"old": {"first_seen": "y"}})

    def boom(folder):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(core.downloader, "scan_playlist_folder", boom)
    core._note_unavailable_songs(plan, folder, ["x"], {"x": "new one"},
                                 {"old", "x"}, report)
    # Scan failure is tolerated: nothing pruned, but nothing crashed either.
    notes = core._read_unavailable_notes(folder)
    assert set(notes) == {"old", "x"}
    assert not report.errors


def test_clean_temp_files_non_dir_and_oserror(monkeypatch, tmp_path):
    core._clean_temp_files(tmp_path / "missing")  # early return

    folder = tmp_path / "f"
    folder.mkdir()
    (folder / "x.part").write_bytes(b"")
    (folder / "ok.mp3").write_bytes(b"")

    def raising_unlink(self, *a, **k):
        raise OSError("locked")

    monkeypatch.setattr(type(folder / "x.part"), "unlink", raising_unlink)
    core._clean_temp_files(folder)  # OSError swallowed
    assert (folder / "ok.mp3").exists()


def test_sync_records_unavailable_after_full_run(monkeypatch, tmp_path):
    root = tmp_path / "mp3s"
    cfg = make_config(root)
    remote = [RemotePlaylist("PLn", "New", "https://y/playlist?list=PLn")]
    monkeypatch.setattr(core, "discover_remote_playlists", lambda cfg: remote)
    monkeypatch.setattr(core, "_remote_entries", lambda cfg, url: [
        entry("ghost", "Ghosted", available=False)
    ])
    monkeypatch.setattr(
        core.downloader, "sync_playlist",
        lambda f, t, e, s, log=None: {"added": 0, "failed": [], "renamed": 0},
    )
    report = sync(cfg)
    assert any("Ghosted" in x for x in report.unavailable)
    notes = core._read_unavailable_notes(root / "New")
    assert "ghost" in notes


def test_reconcile_orphans_archive_and_delete_policies(monkeypatch, tmp_path):
    from ytmusic_mirror import downloader as d

    real_scan = d.scan_playlist_folder
    for policy in ("archive", "delete"):
        root = tmp_path / policy
        cfg = make_config(root, orphan_policy=policy)
        folder = write_folder(root, "Play", "PLp")
        tag_file(folder / "1. Only-oo.mp3", "oo", 1, "Only")
        files = real_scan(folder)
        monkeypatch.setattr(core.downloader, "scan_playlist_folder",
                            lambda f, files=files: files)
        report = core.SyncReport()
        plan = core.PlaylistPlan("PLp", "Play", "", "update", folder="Play")
        core._reconcile_orphans(cfg, plan, ["oo"], report, core.Logger(lambda m: None))
        if policy == "archive":
            assert (cfg.effective_archive_dir / "delisted" / "PLp" / "1. Only-oo.mp3").exists()
            assert not (folder / "1. Only-oo.mp3").exists()
        else:
            assert not (folder / "1. Only-oo.mp3").exists()


def _execute_update_with(cfg, folder, monkeypatch, scan=None):
    """Drive _execute_plan for an existing folder with network mocked out."""
    monkeypatch.setattr(core, "_remote_entries", lambda cfg, url: [])
    plan = core.PlaylistPlan("PLu", "List", "https://y/playlist?list=PLu",
                             "update", folder=folder)
    report = SyncReport()
    if scan is not None:
        monkeypatch.setattr(core.downloader, "scan_playlist_folder", scan)
    core._execute_plan(cfg, plan, dry_run=False, report=report,
                       log=core.Logger(lambda m: None), seq=None)
    return report


def test_execute_update_missing_config(tmp_path, monkeypatch):
    root = tmp_path / "mp3s"
    (root / "Some").mkdir(parents=True)  # folder but NO config file
    cfg = make_config(root)
    report = _execute_update_with(cfg, "Some", monkeypatch)
    assert report.errors and not report.updated


def test_execute_update_scan_error(tmp_path, monkeypatch):
    root = tmp_path / "mp3s"
    write_folder(root, "Some", "PLu")
    cfg = make_config(root)

    def boom(folder):
        raise RuntimeError("scan broke")

    report = _execute_update_with(cfg, "Some", monkeypatch, scan=boom)
    assert report.errors and report.skipped_playlists == ["List"]


def test_execute_rename_oserror(tmp_path, monkeypatch):
    from pathlib import Path as P

    root = tmp_path / "mp3s"
    write_folder(root, "Old", "PLu")
    cfg = make_config(root)
    plan = core.PlaylistPlan("PLu", "Brand New", "https://y/playlist?list=PLu",
                             "update", folder="Old")
    report = SyncReport()

    def raise_rename(self, target, *a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(P, "rename", raise_rename)
    monkeypatch.setattr(core, "_remote_entries", lambda cfg, url: [])
    core._execute_plan(cfg, plan, dry_run=False, report=report,
                       log=core.Logger(lambda m: None), seq=None)
    assert any("Could not rename" in e for e in report.errors)


def test_execute_rename_retag_failure(tmp_path, monkeypatch):
    root = tmp_path / "mp3s"
    folder = write_folder(root, "Old", "PLu")
    tag_file(folder / "1. Alpha-aa.mp3", "aa", 1, "Alpha", "Old")
    cfg = make_config(root)
    plan = core.PlaylistPlan("PLu", "New Name", "https://y/playlist?list=PLu",
                             "update", folder="Old")
    report = SyncReport()

    def bad_retag(path, title):
        raise RuntimeError("mutagen broke")

    monkeypatch.setattr(core.downloader, "update_album_tag", bad_retag)
    monkeypatch.setattr(core, "_remote_entries", lambda cfg, url: [])
    monkeypatch.setattr(
        core.downloader, "sync_playlist",
        lambda f, t, e, s, log=None: {"added": 0, "failed": [], "renamed": 0},
    )
    core._execute_plan(cfg, plan, dry_run=False, report=report,
                       log=core.Logger(lambda m: None), seq=None)
    assert any("Could not retag" in w for w in report.warnings)
    assert (root / "New Name").is_dir()  # rename still succeeded


def test_handle_deleted_delete_and_archive_oserror(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, deleted_playlist_policy="delete")
    folder = write_folder(tmp_path, "Gone", "PLx")
    report = SyncReport()

    def fail_rmtree(path, *a, **k):
        raise OSError("rmtree failed")

    monkeypatch.setattr(core.shutil, "rmtree", fail_rmtree)
    plan = core.PlaylistPlan("PLx", "Gone", "", "archive_deleted", folder="Gone")
    core._handle_deleted_playlist(cfg, plan, dry_run=False, report=report,
                                  log=core.Logger(lambda m: None))
    assert any("Could not delete" in e for e in report.errors)
    assert folder.exists()

    cfg2 = make_config(tmp_path, deleted_playlist_policy="archive")
    folder2 = write_folder(tmp_path, "Gone2", "PLx")

    def fail_move(src, dst, *a, **k):
        raise OSError("move failed")

    monkeypatch.setattr(core.shutil, "move", fail_move)
    plan2 = core.PlaylistPlan("PLx", "Gone2", "", "archive_deleted", folder="Gone2")
    core._handle_deleted_playlist(cfg2, plan2, dry_run=False, report=report,
                                  log=core.Logger(lambda m: None))
    assert any("Could not archive" in e for e in report.errors)
    assert folder2.exists()


def test_reconcile_unlink_and_move_oserror(tmp_path, monkeypatch):
    from pathlib import Path as P
    from ytmusic_mirror import downloader as d

    folder = write_folder(tmp_path, "Play", "PLp")
    tag_file(folder / "1. A-aa.mp3", "aa", 1, "A")
    files = d.scan_playlist_folder(folder)

    def fail_unlink(self, *a, **k):
        raise OSError("locked")

    monkeypatch.setattr(P, "unlink", fail_unlink)
    monkeypatch.setattr(core.downloader, "scan_playlist_folder", lambda f: files)
    cfg = make_config(tmp_path, orphan_policy="delete")  # avoids network probing
    report = SyncReport()
    plan = core.PlaylistPlan("PLp", "Play", "", "update", folder="Play")
    core._reconcile_orphans(cfg, plan, ["aa"], report, core.Logger(lambda m: None))
    assert any("Could not delete" in e for e in report.errors)
    assert (folder / "1. A-aa.mp3").exists()


def test_reconcile_scan_and_archive_move_errors(tmp_path, monkeypatch):
    from ytmusic_mirror import downloader as d

    folder = write_folder(tmp_path, "Play", "PLp")
    tag_file(folder / "1. A-aa.mp3", "aa", 1, "A")
    cfg = make_config(tmp_path, orphan_policy="archive")
    plan = core.PlaylistPlan("PLp", "Play", "", "update", folder="Play")
    real_scan = d.scan_playlist_folder

    # scan error
    def boom(folder):
        raise RuntimeError("scan broke")

    monkeypatch.setattr(core.downloader, "scan_playlist_folder", boom)
    report = SyncReport()
    core._reconcile_orphans(cfg, plan, ["aa"], report, core.Logger(lambda m: None))
    assert any("Could not scan" in e for e in report.errors)

    # archive move error
    def fail_move(src, dst, *a, **k):
        raise OSError("move failed")

    monkeypatch.setattr(core.downloader, "scan_playlist_folder", lambda f: real_scan(f))
    monkeypatch.setattr(core.shutil, "move", fail_move)
    report2 = SyncReport()
    core._reconcile_orphans(cfg, plan, ["aa"], report2, core.Logger(lambda m: None))
    assert any("Could not archive" in e for e in report2.errors)
    assert (folder / "1. A-aa.mp3").exists()
