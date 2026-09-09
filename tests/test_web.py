"""Offline tests for the web dashboard (FastAPI app)."""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ytmusic_mirror.config import Config, write_default_config
from ytmusic_mirror.web import Server, create_app


@pytest.fixture()
def server_and_client(tmp_path):
    cfg_file = tmp_path / "config.json"
    music = tmp_path / "music"
    write_default_config(cfg_file, music_dir=music)
    server = Server(cfg_file)
    client = TestClient(create_app(server))
    yield server, client, cfg_file, music
    server.scheduler.shutdown(wait=False)


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_status_and_index(server_and_client):
    _, client, _, music = server_and_client
    page = client.get("/")
    assert page.status_code == 200 and "ytmusic-mirror" in page.text
    # The inline JS must not contain Python-mangled quote escapes, or the whole
    # page's script (and every button) silently stops working.
    script = re.search(r"<script>(.*?)</script>", page.text, re.S).group(1)
    assert "\\'" not in script

    status = client.get("/api/status").json()
    assert status["running"] is False
    assert status["config"]["music_dir"] == str(music.resolve())
    assert status["config"]["scheduler"]["enabled"] is False


def test_manage_channel_and_playlists(server_and_client):
    server, client, cfg_file, _ = server_and_client
    r = client.post("/api/sources", json={"action": "set-channel", "url": "@myhandle"})
    assert r.status_code == 200
    assert r.json()["channel_url"] == "https://www.youtube.com/@myhandle"

    r = client.post("/api/sources", json={"action": "add-playlist",
                                          "url": "https://y/playlist?list=PLx"})
    assert r.status_code == 200
    assert r.json()["playlists"] == ["https://y/playlist?list=PLx"]

    # bad source input -> 400
    r = client.post("/api/sources", json={"action": "add-playlist",
                                          "url": "https://y/watch?v=xx"})
    assert r.status_code == 400

    # remove keeps channel
    r = client.post("/api/sources", json={"action": "remove-playlist",
                                          "url": "https://y/playlist?list=PLx"})
    assert r.status_code == 200 and r.json()["playlists"] == []

    # persisted to disk
    cfg = Config.load(cfg_file)
    assert cfg.channel_url == "https://www.youtube.com/@myhandle"

    r = client.post("/api/sources", json={"action": "clear-channel"})
    assert r.status_code == 200 and r.json()["channel_url"] == ""


def test_settings_and_scheduler(server_and_client):
    server, client, _, _ = server_and_client
    r = client.patch("/api/settings", json={
        "orphan_policy": "delete",
        "deleted_playlist_policy": "keep",
        "remote_components": True,
        "scheduler": {"enabled": True, "cron": "*/5 * * * *"},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["orphan_policy"] == "delete"
    assert body["remote_components"] is True
    assert body["scheduler"]["enabled"] is True
    assert body["scheduler"]["next_run"] is not None

    # invalid cron does not crash the server
    r = client.patch("/api/settings", json={
        "scheduler": {"enabled": True, "cron": "not a cron"},
    })
    assert r.status_code == 200
    assert server.scheduler_state()["next_run"] is None

    # disable again
    r = client.patch("/api/settings", json={
        "scheduler": {"enabled": False, "cron": "0 0 * * *"},
    })
    assert r.json()["scheduler"]["enabled"] is False


def test_sync_run_and_logs(server_and_client, monkeypatch):
    server, client, _, _ = server_and_client
    calls = {}

    def fake_sync(cfg, dry_run=False, log=None):
        calls["dry_run"] = dry_run
        log.info("one")
        log.info("two")
        from ytmusic_mirror.core import SyncReport

        report = SyncReport()
        report.created.append("X")
        return report

    monkeypatch.setattr("ytmusic_mirror.web.run_sync", fake_sync)

    r = client.post("/api/sync", json={"dry_run": True})
    assert r.status_code == 200
    assert wait_for(lambda: not server.sync_active)

    logs = client.get("/api/logs").json()
    assert any("one" in line for line in logs["logs"])
    assert calls["dry_run"] is True

    status = client.get("/api/status").json()
    assert "Created playlists" in (status["last_sync"]["report"] or "")


def test_sync_conflict_409(server_and_client, monkeypatch):
    server, client, _, _ = server_and_client

    def slow_sync(cfg, dry_run=False, log=None):
        time.sleep(0.5)
        from ytmusic_mirror.core import SyncReport

        return SyncReport()

    monkeypatch.setattr("ytmusic_mirror.web.run_sync", slow_sync)
    assert client.post("/api/sync", json={}).status_code == 200
    # second start while first is still running -> 409
    r = client.post("/api/sync", json={})
    assert r.status_code == 409
    assert wait_for(lambda: not server.sync_active)


def test_discover_endpoint(server_and_client, monkeypatch):
    _, client, _, _ = server_and_client
    from ytmusic_mirror.core import RemotePlaylist

    monkeypatch.setattr(
        "ytmusic_mirror.core.discover_remote_playlists",
        lambda cfg: [RemotePlaylist("PL1", "One", "https://y/playlist?list=PL1")],
    )
    r = client.post("/api/discover")
    assert r.status_code == 200
    assert r.json()["remote"] == [{"id": "PL1", "title": "One",
                                   "url": "https://y/playlist?list=PL1"}]


def test_config_scheduler_roundtrip(tmp_path):
    cfg = Config(music_dir=tmp_path / "m", scheduler_enabled=True, scheduler_cron="*/5 * * * *")
    path = tmp_path / "c.json"
    cfg.save(path)
    again = Config.load(path)
    assert again.scheduler_enabled is True
    assert again.scheduler_cron == "*/5 * * * *"
