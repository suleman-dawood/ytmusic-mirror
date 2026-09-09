# ytmusic-mirror

Keep a local folder of MP3s in sync with your **public YouTube Music playlists**.
Each playlist becomes a folder; songs, order and folders all follow your account.

Works on **Windows and Linux**.

[![PyPI](https://img.shields.io/pypi/v/ytmusic-mirror?color=ff1a1a&label=PyPI)](https://pypi.org/project/ytmusic-mirror/)
[![GitHub Release](https://img.shields.io/github/v/release/suleman-dawood/ytmusic-mirror?color=ff1a1a)](https://github.com/suleman-dawood/ytmusic-mirror/releases)
[![Docker Hub](https://img.shields.io/docker/pulls/sulemandawood/ytmusic-mirror?color=ff1a1a&label=Docker%20Hub)](https://hub.docker.com/u/sulemandawood)
[![GHCR](https://img.shields.io/badge/GHCR-ghcr.io-ff1a1a)](https://github.com/suleman-dawood/ytmusic-mirror/pkgs/container/ytmusic-mirror)

```
~/Music/MP3s/                    <- your master folder
├── Road Trip/
│   ├── 01. Kickstart My Heart-Motley Crue-9lQ7.mp3
│   └── .playlist_config.json
├── Gym Mix/
└── _Archive/                    <- safe-keeps everything removed from your account
```

## What it does

- Discovers your playlists (from your channel or an explicit list of URLs).
- **New playlist** → creates a folder and downloads it.
- **Playlist renamed** → renames the local folder (and album tags).
- **Playlist deleted** → moves the folder into `_Archive/deleted/<id>`.
- **Song added / reordered** → downloaded / renumbered into place.
- **Song you removed** (video still on YouTube) → deleted locally.
- **Song delisted by YouTube** (video gone/private) → kept in `_Archive/delisted/<id>`.
- **Song listed but currently unavailable** → recorded in
  `<playlist>/.ytmusic-mirror.json` and shown in the summary as "listed as
  unavailable". It is never miscounted as a new download, and is retried on
  later syncs until it becomes downloadable (then the note is dropped).
- **Web dashboard (optional)** → a small self-hosted UI to manage sources, run
  syncs with live logs, schedule them with simple presets (no cron knowledge
  needed), and inspect what is on disk.

If unsure, run `sync --dry-run` first, or set `orphan_policy` to `archive`
to never delete anything. Results are printed as a vertical summary list.

## Requirements

- Python 3.9+
- `ffmpeg` on your `PATH`

Everything else is installed automatically with the package.

## Install

Pick whichever channel suits you:

```sh
# Python package (PyPI)
pip install ytmusic-mirror            # core CLI
pip install "ytmusic-mirror[web]"     # + web dashboard (fastapi/uvicorn/apscheduler)

# Homebrew (macOS / Linux)
brew tap suleman-dawood/homebrew-ytmusic
brew install ytmusic-mirror

# Docker — GHCR or Docker Hub (both self-updating via CI on tags)
docker run --rm -p 8000:8000 -v ~/Music/MP3s:/music ghcr.io/suleman-dawood/ytmusic-mirror
docker run --rm -p 8000:8000 -v ~/Music/MP3s:/music sulemandawood/ytmusic-mirror

# From source
git clone https://github.com/suleman-dawood/ytmusic-mirror
cd ytmusic-mirror && pip install .
```

See [docs/RELEASING.md](docs/RELEASING.md) for how new versions are published to
each channel.

## Quick start

Everything is configured from the terminal — no manual file editing.

```sh
# 1. Create a config, telling it where your master folder is.
ytmusic-mirror init --dir "~/Music/MP3s"          # Linux/macOS
ytmusic-mirror init --dir "%USERPROFILE%\Music\MP3s"   # Windows

# 2. Tell it what to mirror:
#    - your channel (all its public playlists are auto-discovered), or
#    - specific playlist URLs
ytmusic-mirror channel "https://www.youtube.com/@YourHandle"   # or just "@YourHandle"
ytmusic-mirror add "https://music.youtube.com/playlist?list=..."   # repeatable

# 3. Sync
ytmusic-mirror sync                     # update everything
ytmusic-mirror sync --dry-run           # preview only, changes nothing
ytmusic-mirror sync --dir "/some/else"  # override the master folder
ytmusic-mirror remote                   # list what would be synced
```

Manage what you've configured:

```sh
ytmusic-mirror status               # show channel, playlists, folder, policies
ytmusic-mirror channel              # show the configured channel
ytmusic-mirror channel --clear      # remove the channel
ytmusic-mirror set-dir "~/Music/MP3s"     # change the master folder
ytmusic-mirror remove "<playlist-url>"   # stop syncing one playlist
```

Paths: use an absolute path or `~/...` (note the slash — `~Music/...` is
invalid and is rejected with a hint). Only `~`, `~/` and `~\` are treated as
home references; other `~user/...` forms are not supported.

### yt-dlp "Signature solving failed" / EJS warnings

If a sync shows warnings like `Signature solving failed: Some formats may be
missing` or `Remote components ... were skipped`, YouTube is requiring yt-dlp
to solve a JS challenge. Allow yt-dlp to fetch its solver by adding this to the
config (a JS runtime such as Deno or Node must be installed):

```json
"remote_components": ["ejs:github"]
```

The config file lives at `%APPDATA%\ytmusic-mirror\config.json` on Windows and
`~/.config/ytmusic-mirror/config.json` (or `$XDG_CONFIG_HOME`) on Linux. You can
point to another one with `-c <path>` on every command.

## Web dashboard

An optional single-page dashboard for managing the mirror from a browser
(yubal-style). It is download-manager only - playback is left to your own media
server pointed at the same music folder.

```sh
pip install "ytmusic-mirror[web]"
ytmusic-mirror serve                 # http://localhost:8000
ytmusic-mirror serve --host 0.0.0.0  # expose to your LAN
ytmusic-mirror serve -c <path> -d <master-folder>
```

From the UI you can set your channel, add/remove playlists, discover channel
playlists, run a sync (with live logs and the summary report), view what is on
disk, change policies, and schedule automatic syncs with plain-English presets
(every day/week / every N minutes / custom cron under "advanced"). Settings are
written to the same config file the CLI uses, so both work side by side.

> The dashboard has **no built-in authentication** - keep it on `localhost` or
> put a reverse proxy with auth in front before exposing it.

## Docker

A ready-made image is published to
`ghcr.io/suleman-dawood/ytmusic-mirror` (see
[`examples/docker-compose.yml`](examples/docker-compose.yml)). It bundles
ffmpeg and Deno (for yt-dlp's JS challenge solving).

```sh
mkdir -p music config
docker compose -f examples/docker-compose.yml up -d
# open http://localhost:8000
```

- `./music` = your MP3 mirror (point your player here)
- `./config` = config + per-playlist state (persisted)
- On first boot it creates a default config pointing at `/music`; on every
  restart it picks up where it left off (safe to kill, syncs resume).
- `docker pull ghcr.io/suleman-dawood/ytmusic-mirror` for the standalone image.

## Interrupted or large downloads?

No problem. Sync works playlist-by-playlist and downloads songs one at a time;
finished songs and playlists are never re-done. If you stop the process (Ctrl+C,
power loss, crash) just run the same command again — it resumes where it left
off. Any leftover `.part`/`.tmp` files from an interrupted download are cleaned
up automatically on the next run.

## Logs

Normal mode shows live progress (which playlist, per-song download bars) and a
summary at the end.

```sh
ytmusic-mirror sync            # show everything
ytmusic-mirror sync --nolog    # quiet: no progress lines, summary + errors only
```

## Config options

| Key | Default | Meaning |
| --- | --- | --- |
| `music_dir` | `~/Music/MP3s` | Master folder playlists are synced into |
| `channel_url` | `""` | Your channel → public playlists are auto-discovered |
| `playlists` | `[]` | Extra playlist URLs to mirror |
| `cookies_from_browser` / `cookie_file` | `""` | For private / age-restricted content |
| `remote_components` | `[]` | Passed to yt-dlp. Set to `["ejs:github"]` to allow yt-dlp to fetch its JS challenge solver |
| `archive_dir` | `<music_dir>/_Archive` | Where removed/delisted things go |
| `deleted_playlist_policy` | `archive` | `archive` \| `delete` \| `keep` |
| `orphan_policy` | `smart` | `smart` (delete removed, archive delisted) \| `archive` \| `delete` |
| `scheduler_enabled` | `false` | Auto-run a sync on a schedule (used by the web dashboard) |
| `scheduler_cron` | `0 0 * * *` | Cron expression when scheduling is enabled |
| `download` | `{}` | Extra settings for new playlists (codec, naming, ...) |

## Tests

```sh
pip install -e ".[dev]"
pytest                              # offline suite (99% line coverage)
pytest --cov=ytmusic_mirror         # with coverage report
YT_MIRROR_LIVE=1 pytest -m live     # optional real-download tests
```

The offline suite has no network requirements and runs in CI on Linux and
Windows across Python 3.10-3.13 (`.github/workflows/ci.yml`). Real downloads
against YouTube are covered by the opt-in `live` tests.

## License

MIT.

ytmusic-mirror is a self-contained tool built directly on the open libraries
[yt-dlp](https://github.com/yt-dlp/yt-dlp) and
[mutagen](https://github.com/quodlibet/mutagen) — it has no dependency on any
third-party downloader application, so upstream changes to other projects
cannot break it.

For personal archiving only. Respect YouTube's Terms of Service and applicable
copyright law.
