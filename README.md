# ytmusic-mirror

Keep a local folder of MP3s in sync with your **public YouTube Music playlists**.
Each playlist becomes a folder; songs, order and folders all follow your account.

Works on **Windows and Linux**.

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

If unsure, run `sync --dry-run` first, or set `orphan_policy` to `archive`
to never delete anything.

## Requirements

- Python 3.9+
- `ffmpeg` on your `PATH`

Everything else is installed automatically with the package.

## Install

```sh
pip install .
# or, once on PyPI:
# pip install ytmusic-mirror
```

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
ytmusic-mirror remove "<playlist-url>"   # stop syncing one playlist
```

The config file lives at `%APPDATA%\ytmusic-mirror\config.json` on Windows and
`~/.config/ytmusic-mirror/config.json` (or `$XDG_CONFIG_HOME`) on Linux. You can
point to another one with `-c <path>` on every command.

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
| `archive_dir` | `<music_dir>/_Archive` | Where removed/delisted things go |
| `deleted_playlist_policy` | `archive` | `archive` \| `delete` \| `keep` |
| `orphan_policy` | `smart` | `smart` (delete removed, archive delisted) \| `archive` \| `delete` |
| `download` | `{}` | Extra settings for new playlists (codec, naming, ...) |

## Tests

```sh
pip install -e .[dev]
pytest
```

## License

MIT. Contains a vendored copy of
[youtube_music_playlist_downloader](https://github.com/onnowhere/youtube_music_playlist_downloader)
(c) 2022 onnowhere, MIT — see `ytmusic_mirror/vendor/UPSTREAM_LICENSE.txt`.

For personal archiving only. Respect YouTube's Terms of Service and applicable
copyright law.
