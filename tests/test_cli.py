"""CLI tests: channel/add/remove/status config management via the command line."""

from ytmusic_mirror.cli import _normalize_channel, main
from ytmusic_mirror.config import Config


def run(capsys, *argv):
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_init_and_status(capsys, tmp_path):
    cfg_file = tmp_path / "cfg.json"
    code, out, _ = run(capsys, "init", "-c", str(cfg_file), "--dir", str(tmp_path / "mp3"))
    assert code == 0 and "Wrote default config" in out
    code, out, _ = run(capsys, "status", "-c", str(cfg_file))
    assert code == 0
    assert str(tmp_path / "mp3") in out
    assert "(not set)" in out  # no channel yet


def test_channel_bare_handle(capsys, tmp_path):
    cfg_file = tmp_path / "cfg.json"
    run(capsys, "init", "-c", str(cfg_file))
    code, out, _ = run(capsys, "channel", "-c", str(cfg_file), "@mychannel")
    assert code == 0
    cfg = Config.load(cfg_file)
    assert cfg.channel_url == "https://www.youtube.com/@mychannel"


def test_channel_rejects_playlist_url(capsys, tmp_path):
    cfg_file = tmp_path / "cfg.json"
    run(capsys, "init", "-c", str(cfg_file))
    code, out, err = run(
        capsys, "channel", "-c", str(cfg_file),
        "https://www.youtube.com/playlist?list=PLabc",
    )
    assert code == 1
    assert "playlist" in err.lower()
    assert Config.load(cfg_file).channel_url == ""


def test_channel_show_and_clear(capsys, tmp_path):
    cfg_file = tmp_path / "cfg.json"
    run(capsys, "init", "-c", str(cfg_file))
    run(capsys, "channel", "-c", str(cfg_file), "https://www.youtube.com/@x")
    _, out, _ = run(capsys, "channel", "-c", str(cfg_file))
    assert "@x" in out
    code, _, _ = run(capsys, "channel", "-c", str(cfg_file), "--clear")
    assert code == 0
    assert Config.load(cfg_file).channel_url == ""


def test_add_validates_playlist(capsys, tmp_path):
    cfg_file = tmp_path / "cfg.json"
    run(capsys, "init", "-c", str(cfg_file))
    code, _, err = run(capsys, "add", "-c", str(cfg_file), "https://example.com/not-a-playlist")
    assert code == 1 and "list" in err.lower()
    assert Config.load(cfg_file).playlists == []

    code, out, _ = run(
        capsys, "add", "-c", str(cfg_file),
        "https://music.youtube.com/playlist?list=PLabc&x=1",
    )
    assert code == 0 and "Added 1" in out
    assert Config.load(cfg_file).playlists == [
        "https://music.youtube.com/playlist?list=PLabc&x=1"
    ]


def test_add_duplicate_rejected(capsys, tmp_path):
    cfg_file = tmp_path / "cfg.json"
    run(capsys, "init", "-c", str(cfg_file))
    url = "https://www.youtube.com/playlist?list=PLabc"
    run(capsys, "add", "-c", str(cfg_file), url)
    code, _, err = run(capsys, "add", "-c", str(cfg_file), url)
    assert code == 1 and "Already added" in err
    assert Config.load(cfg_file).playlists == [url]


def test_remove(capsys, tmp_path):
    cfg_file = tmp_path / "cfg.json"
    run(capsys, "init", "-c", str(cfg_file))
    run(capsys, "add", "-c", str(cfg_file), "https://youtube.com/playlist?list=PLabc")
    code, out, _ = run(capsys, "remove", "-c", str(cfg_file), "https://youtube.com/playlist?list=PLabc")
    assert code == 0 and "Removed 1" in out
    assert Config.load(cfg_file).playlists == []


def test_normalize_channel_rejects_bad():
    import pytest

    assert _normalize_channel("@foo") == "https://www.youtube.com/@foo"
    with pytest.raises(ValueError):
        _normalize_channel("just some words")
    with pytest.raises(ValueError):
        _normalize_channel("https://www.youtube.com/playlist?list=PLabc")


def test_set_dir_updates_path_keeps_channel(capsys, tmp_path):
    cfg_file = tmp_path / "cfg.json"
    run(capsys, "init", "-c", str(cfg_file))
    run(capsys, "channel", "-c", str(cfg_file), "@me")
    code, out, _ = run(capsys, "set-dir", "-c", str(cfg_file), str(tmp_path / "new"))
    assert code == 0
    cfg = Config.load(cfg_file)
    assert cfg.music_dir == (tmp_path / "new").resolve()
    assert cfg.channel_url.endswith("@me")


def test_set_dir_rejects_tilde_typo(capsys, tmp_path):
    cfg_file = tmp_path / "cfg.json"
    run(capsys, "init", "-c", str(cfg_file))
    code, _, err = run(capsys, "set-dir", "-c", str(cfg_file), "~Music/MP3s")
    assert code == 1
    assert "Did you mean" in err


def test_init_rejects_tilde_typo(capsys, tmp_path):
    code, _, err = run(capsys, "init", "-c", str(tmp_path / "c.json"), "--dir", "~Music/MP3s")
    assert code == 1
    assert "Did you mean" in err


def test_remote_ok_and_json(capsys, tmp_path, monkeypatch):
    from ytmusic_mirror.core import RemotePlaylist
    from ytmusic_mirror import cli as cli_mod

    cfg_file = tmp_path / "cfg.json"
    run(capsys, "init", "-c", str(cfg_file))
    remote = [RemotePlaylist("PL1", "One", "https://y/playlist?list=PL1")]
    monkeypatch.setattr(cli_mod, "discover_remote_playlists", lambda cfg: remote)

    code, out, _ = run(capsys, "remote", "-c", str(cfg_file))
    assert code == 0 and "PL1\tOne" in out
    code, out, _ = run(capsys, "remote", "--json", "-c", str(cfg_file))
    assert '"id": "PL1"' in out


def test_remote_failure_exits_1(capsys, tmp_path, monkeypatch):
    from ytmusic_mirror import cli as cli_mod

    cfg_file = tmp_path / "cfg.json"
    run(capsys, "init", "-c", str(cfg_file))

    def boom(cfg):
        raise RuntimeError("discovery exploded")

    monkeypatch.setattr(cli_mod, "discover_remote_playlists", boom)
    code, _, err = run(capsys, "remote", "-c", str(cfg_file))
    assert code == 1 and "Discovery failed" in err


def test_sync_prints_report_and_exit_codes(capsys, tmp_path, monkeypatch):
    from ytmusic_mirror import cli as cli_mod
    from ytmusic_mirror.core import SyncReport

    cfg_file = tmp_path / "cfg.json"
    run(capsys, "init", "-c", str(cfg_file), "--dir", str(tmp_path / "m"))

    calls = {}

    def fake_sync(cfg, dry_run=False, log=None):
        calls["dry_run"] = dry_run
        calls["log_quiet"] = log.quiet
        report = SyncReport()
        if dry_run:
            report.created.append("A")
        else:
            report.errors.append("boom")
        return report

    monkeypatch.setattr(cli_mod, "sync", fake_sync)

    code, out, _ = run(capsys, "sync", "--dry-run", "-c", str(cfg_file))
    assert code == 0
    assert calls["dry_run"] is True and "DRY RUN" in out and "A" in out

    code, _, err = run(capsys, "sync", "-c", str(cfg_file))
    assert code == 1
    assert calls["log_quiet"] is False

    code, _, _ = run(capsys, "sync", "--nolog", "-c", str(cfg_file))
    assert code == 1 and calls["log_quiet"] is True


def test_sync_keyboard_interrupt(capsys, tmp_path, monkeypatch):
    from ytmusic_mirror import cli as cli_mod

    cfg_file = tmp_path / "cfg.json"
    run(capsys, "init", "-c", str(cfg_file), "--dir", str(tmp_path / "m"))

    def raise_interrupt(cfg, dry_run=False, log=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "sync", raise_interrupt)
    code, _, err = run(capsys, "sync", "-c", str(cfg_file))
    assert code == 130 and "Interrupted" in err


def test_init_existing_needs_force(capsys, tmp_path):
    cfg_file = tmp_path / "cfg.json"
    assert run(capsys, "init", "-c", str(cfg_file))[0] == 0
    code, out, _ = run(capsys, "init", "-c", str(cfg_file))
    assert code == 1 and "already exists" in out.lower()


def test_status_without_config(capsys, tmp_path):
    code, out, _ = run(capsys, "status", "-c", str(tmp_path / "missing.json"))
    assert code == 0 and "No config yet" in out


def test_remove_channel_and_no_match(capsys, tmp_path):
    cfg_file = tmp_path / "cfg.json"
    run(capsys, "init", "-c", str(cfg_file))
    run(capsys, "channel", "-c", str(cfg_file), "@u")
    code, out, _ = run(capsys, "remove", "-c", str(cfg_file), "--channel")
    assert code == 0
    assert Config.load(cfg_file).channel_url == ""

    code, out, _ = run(capsys, "remove", "-c", str(cfg_file),
                       "https://youtube.com/playlist?list=PLnope")
    assert code == 0 and "No matching playlists" in out

    code, _, err = run(capsys, "remove", "-c", str(cfg_file))
    assert code == 1 and "Nothing to remove" in err


def test_init_force_overwrites(capsys, tmp_path):
    cfg_file = tmp_path / "cfg.json"
    run(capsys, "init", "-c", str(cfg_file), "--dir", str(tmp_path / "one"))
    run(capsys, "channel", "-c", str(cfg_file), "@before")
    code, out, _ = run(capsys, "init", "--force", "-c", str(cfg_file),
                       "--dir", str(tmp_path / "two"))
    assert code == 0
    cfg = Config.load(cfg_file)
    assert cfg.music_dir == (tmp_path / "two").resolve()
    assert cfg.channel_url == ""  # overwritten fresh


def test_sync_missing_config_clean_error(capsys, tmp_path):
    code, _, err = run(capsys, "sync", "-c", str(tmp_path / "missing.json"))
    assert code == 1 and "No config file" in err


def test_sync_respects_dir_override(capsys, tmp_path, monkeypatch):
    from ytmusic_mirror import cli as cli_mod

    cfg_file = tmp_path / "cfg.json"
    run(capsys, "init", "-c", str(cfg_file), "--dir", str(tmp_path / "base"))
    seen = {}

    def fake_sync(cfg, dry_run=False, log=None):
        seen["dir"] = cfg.music_dir
        from ytmusic_mirror.core import SyncReport
        return SyncReport()

    monkeypatch.setattr(cli_mod, "sync", fake_sync)
    override = tmp_path / "elsewhere"
    code, _, _ = run(capsys, "sync", "-d", str(override), "-c", str(cfg_file))
    assert code == 0 and seen["dir"] == override.resolve()
