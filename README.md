# ytmusic-mirror

Mirror your **public YouTube Music playlists** into a local folder of MP3
albums, keeping the folder structure and its songs in lock-step with your
account.

```
~/Music/MP3s/
├── Road Trip/
│   ├── 01. Kickstart My Heart-Motley Crue-9lQ7.mp3
│   ├── 02. Panama-Van Halen-abc12.mp3
│   └── .playlist_config.json
├── Gym Mix/
│   └── ...
└── _Archive/          ← everything removed from your account lands here
```

It is a thin, non-interactive driver around the excellent
[YouTube Music Playlist Downloader](https://github.com/onnowhere/youtube_music_playlist_downloader)
(which is vendored here under MIT), adding the account-level and cleanup
behaviour that tool's interactive menu does not provide:

| Behaviour | ytmusic-mirror |
| --- | --- |
| New playlist on your account | creates a matching folder and downloads it |
| Playlist renamed on your account | renames the local folder (and album tags) |
| Playlist deleted from your account | moves the folder to `_Archive/deleted/<id>` |
| Song added to a playlist | downloads it into the folder |
| Song reordered on the playlist | renumbers/reorders the local files |
| Song removed from a playlist, video still up | **deletes** the local file |
| Song removed from a playlist, video **delisted** | keeps a copy in `_Archive/delisted/<id>` |

## How it decides "delisted" vs "removed"

After each playlist sync, any local song whose video id is no longer present in
the remote playlist is probed:

* if the YouTube video is still playable, the song was removed from the
  playlist by you → the local file is **deleted**;
* if the video is gone/private/region-blocked, the track was delisted → the
  file is **moved into `_Archive/delisted/<playlist-id>/`** so you never lose it.

> Only songs **you** remove from a playlist get deleted. Anything YouTube takes
> down is preserved automatically. Run `sync --dry-run` first if you want a
> preview, or set `orphan_policy` to `archive` to never delete anything.

## Requirements

* Python 3.9+
* `yt-dlp`, `mutagen`, `pillow`, `langcodes`, `requests` (installed
  automatically with the package)
* `ffmpeg` available on `PATH`

## Install

```sh
pip install .
# or, from PyPI once published:
# pip install ytmusic-mirror
```

## Quick start

```sh
# 1. create the config (default ~/.config/ytmusic-mirror/config.json)
ytmusic-mirror init

# 2. set your channel in the config (e.g. https://www.youtube.com/@YourHandle),
#    or add specific playlist URLs:
ytmusic-mirror add "https://music.youtube.com/playlist?list=..."
```

`config.json`:

```jsonc
{
  "music_dir": "~/Music/MP3s",
  // Public playlists are discovered from your channel's /playlists tab.
  "channel_url": "https://www.youtube.com/@YourHandle",
  // Optional: extra playlist URLs (e.g. playlists not shown on your channel).
  "playlists": [],
  // Optional, for private/age-restricted content:
  "cookies_from_browser": "",   // e.g. "chromium", "firefox", "chrome"
  "cookie_file": "",
  // Where removed/delisted things go. Defaults to <music_dir>/_Archive.
  "archive_dir": "",
  // What to do when a whole playlist disappears from your account:
  //   archive (default, safe) | delete | keep
  "deleted_playlist_policy": "archive",
  // How to treat songs missing from a remote playlist:
  //   smart (default) delete-if-still-on-YouTube else archive
  //   archive (never delete) | delete (never archive)
  "orphan_policy": "smart",
  // Extra settings merged into freshly created playlist configs
  // (see the YouTube Music Playlist Downloader README for keys).
  "download": {
    "audio_codec": "mp3",
    "track_num_in_name": true,
    "use_title": true
  }
}
```

### Sync

```sh
ytmusic-mirror sync           # update everything
ytmusic-mirror sync --dry-run # preview only, changes nothing
ytmusic-mirror remote         # list the playlists that would be synced
ytmusic-mirror remote --json
```

Run it manually, or schedule it:

```sh
# every night at 02:00 (example cron line)
0 2 * * * /path/to/ytmusic-mirror sync >> ~/.cache/ytmusic-mirror.log 2>&1
```

## Notes

* Folders are identified by playlist id (via the `.playlist_config.json` file
  the downloader keeps in each folder), so renames never cause re-downloads or
  duplicates.
* Playlist folders created here are fully compatible with the interactive
  `youtube-music-downloader` menu if you want to manage one by hand.
* The tool intentionally never touches directories starting with `.` or `_`
  (your `_Archive` lives safely inside the music folder).
* Public **unlisted** playlists work too; **private** ones need
  `cookies_from_browser` / `cookie_file` set.

## Tests

```sh
pip install -e .[dev]
pytest
```

## License

MIT. Contains a vendored copy of
[youtube_music_playlist_downloader](https://github.com/onnowhere/youtube_music_playlist_downloader)
(c) 2022 onnowhere, MIT, see `ytmusic_mirror/vendor/UPSTREAM_LICENSE.txt`.

For personal archiving only. Respect YouTube's Terms of Service and applicable
copyright law.
