"""Native download/tag/order engine for ytmusic-mirror.

Replaces the previously-vendored "youtube-music-downloader" application with
our own implementation built directly on yt-dlp + mutagen, so there is no
third-party app dependency to break on.

File/layout contract kept identical to the old engine so existing mirrors keep
working:

  <playlist folder>/
      01. Title-videoId.mp3      <- mp3 with ID3 tags
      .playlist_config.json      <- our resume/identity config
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import subprocess
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import requests
import yt_dlp
from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TRCK, WOAR
from PIL import Image

CONFIG_VERSION = 2

_INVALID_FILE_CHARS = re.compile(r'[\\/:*?"<>|]')
_TRACK_PREFIX = re.compile(r"^\d+\.\s*")

# Fields mirrored from the downloader's own config schema so both old-style
# (.playlist_config.json written by the upstream app) and our v2 configs work.
_DEFAULTS: Dict[str, object] = {
    "audio_format": "bestaudio/best",
    "audio_codec": "mp3",
    "audio_quality": "5",
    "name_format": "%(title)s-%(id)s.%(ext)s",
    "track_num_in_name": True,
    "use_title": True,
    "use_uploader": True,
    "use_playlist_name": True,
    "reverse_playlist": False,
    "thread_count": 0,
    "verbose": False,
    "cookies_from_browser": "",
    "cookie_file": "",
    "remote_components": [],
    "extractor_args": {},
    "include_metadata": {
        "title": True,
        "artist": True,
        "album": True,
        "track": True,
        "date": True,
        "cover": True,
        "lyrics": False,
        "url": True,
    },
}


def sanitize_name(name: str) -> str:
    """Match the folder/file name sanitisation used by the old engine."""
    return _INVALID_FILE_CHARS.sub("_", name)


def ffmpeg_available() -> bool:
    try:
        subprocess.check_output(["ffmpeg", "-version"])
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def settings_from_config(raw: dict) -> dict:
    """Normalise a stored playlist config into the settings we actually use."""
    merged = dict(_DEFAULTS)
    include = raw.get("include_metadata")
    if include is None:
        include = {k: v for k, v in _DEFAULTS["include_metadata"].items()}  # type: ignore[assignment]
    else:
        include = {
            k: bool(include.get(k, default))
            for k, default in _DEFAULTS["include_metadata"].items()  # type: ignore[union-attr]
        }
    merged["include_metadata"] = include

    def copy(key: str, transform=lambda v: v):
        if key in raw and raw[key] is not None:
            merged[key] = transform(raw[key])

    for key in (
        "url",
        "audio_format",
        "audio_codec",
        "audio_quality",
        "name_format",
        "track_num_in_name",
        "use_title",
        "use_uploader",
        "use_playlist_name",
        "reverse_playlist",
        "thread_count",
        "verbose",
        "cookies_from_browser",
        "cookie_file",
        "extractor_args",
    ):
        copy(key)
    copy("remote_components", lambda v: [str(x) for x in v] if v else [])
    return merged


def new_settings(cfg, url: str) -> dict:
    """Build the settings dict for a brand-new playlist from the app config."""
    settings = dict(_DEFAULTS)
    settings.update(cfg.download or {})  # type: ignore[attr-defined]
    settings["url"] = url
    settings["include_metadata"] = dict(_DEFAULTS["include_metadata"])  # type: ignore[index]
    if cfg.remote_components:  # type: ignore[attr-defined]
        settings["remote_components"] = list(cfg.remote_components)
    if cfg.cookies_from_browser:  # type: ignore[attr-defined]
        settings["cookies_from_browser"] = cfg.cookies_from_browser
    if cfg.cookie_file:  # type: ignore[attr-defined]
        settings["cookie_file"] = cfg.cookie_file
    return settings_from_config(settings)


def write_playlist_config(folder: Path, settings: dict) -> None:
    """Persist our minimal per-playlist config (identity + resume settings)."""
    payload = {"version": CONFIG_VERSION}
    for key in (
        "url",
        "audio_format",
        "audio_codec",
        "audio_quality",
        "name_format",
        "track_num_in_name",
        "use_title",
        "use_uploader",
        "use_playlist_name",
        "reverse_playlist",
        "thread_count",
        "verbose",
        "cookies_from_browser",
        "cookie_file",
        "remote_components",
        "extractor_args",
        "include_metadata",
    ):
        if key in settings:
            payload[key] = settings[key]
    (folder / ".playlist_config.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# Local song scanning
# --------------------------------------------------------------------------- #


@dataclass
class LocalSong:
    video_id: str
    file_name: str
    file_path: Path
    track_num: int
    title: str = ""


def _video_id_from_woar(tags) -> Optional[str]:
    frames = tags.getall("WOAR")
    if not frames or len(frames) > 1:
        return None
    query = parse_qs(urlparse(str(frames[0])).query)
    values = query.get("v")
    return values[0] if values else None


def scan_playlist_folder(folder: Path) -> Dict[str, LocalSong]:
    """Map video id -> song for every tagged audio file in a playlist folder."""
    if not folder.is_dir():
        return {}
    songs: Dict[str, LocalSong] = {}
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.name.startswith((".", "~")):
            continue
        try:
            tags = ID3(path)
        except Exception:
            continue
        video_id = _video_id_from_woar(tags)
        if video_id is None:
            continue
        try:
            track_num = int(str(tags.get("TRCK", 0)))
        except Exception:
            track_num = 0
        title = str(tags.get("TIT2", path.stem))
        if video_id in songs:
            raise RuntimeError(
                "Duplicate song files found in this playlist folder!\n"
                f"  Both '{songs[video_id].file_name}' and '{path.name}' "
                f"link to video id '{video_id}'.\n"
                "Remove the duplicate to continue."
            )
        songs[video_id] = LocalSong(video_id, path.name, path, track_num, title)
    return songs


def update_track_tag(path: Path, track_num: int) -> None:
    tags = ID3(path)
    if str(tags.get("TRCK", "")) != str(track_num):
        tags.add(TRCK(encoding=3, text=str(track_num)))
        tags.save(v2_version=3)


def update_album_tag(path: Path, album: str) -> None:
    tags = ID3(path)
    if str(tags.get("TALB", "")) != album:
        tags.add(TALB(encoding=3, text=album))
        tags.save(v2_version=3)


# --------------------------------------------------------------------------- #
# Download + tagging
# --------------------------------------------------------------------------- #


def _ytdl_opts(settings: dict, outtmpl: str) -> dict:
    opts: dict = {
        "outtmpl": outtmpl,
        "format": settings.get("audio_format", "bestaudio/best"),
        "noplaylist": True,
        "geo_bypass": True,
        "cookiefile": None
        if not settings.get("cookie_file")
        else str(Path(str(settings["cookie_file"])).expanduser()),
        "cookiesfrombrowser": None
        if not settings.get("cookies_from_browser")
        else tuple(str(settings["cookies_from_browser"]).split(":")),
        "extractor_args": settings.get("extractor_args") or {},
        "remote_components": list(settings.get("remote_components") or []),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": settings.get("audio_codec", "mp3"),
                "preferredquality": str(settings.get("audio_quality", "5")),
            }
        ],
    }
    if not settings.get("verbose"):
        opts["quiet"] = True
        opts["external_downloader_args"] = ["-loglevel", "panic"]
    return opts


def download_song(url: str, folder: Path, settings: dict) -> dict:
    """Download one video to <folder>/.ytmirror-<id>.mp3 and return its info."""
    video_id = None
    query = parse_qs(urlparse(url).query)
    if query.get("v"):
        video_id = query["v"][0]
    if not video_id:
        raise ValueError(f"Not a watch URL: {url}")
    tmp_template = str(folder / f".ytmirror-{video_id}.%(ext)s")
    with yt_dlp.YoutubeDL(_ytdl_opts(settings, tmp_template)) as ydl:
        info = ydl.extract_info(url, download=True)
    expected = folder / f".ytmirror-{video_id}.{settings.get('audio_codec', 'mp3')}"
    if not expected.is_file():
        raise RuntimeError("No output file produced by download")
    return info


def _fetch_thumbnail_jpeg(url: str) -> Optional[bytes]:
    try:
        img = Image.open(requests.get(url, stream=True, timeout=20).raw)
    except Exception:
        return None
    try:
        img = img.convert("RGB")
        width, height = img.size
        size = min(width, height)
        left = (width - size) // 2
        top = (height - size) // 2
        img = img.crop((left, top, left + size, top + size))
        with BytesIO() as buf:
            img.save(buf, format="JPEG")
            return buf.getvalue()
    except Exception:
        return None


def _info_field(info: dict, key: str, fallback: str) -> str:
    value = info.get(key)
    return str(value) if value else fallback


def tag_new_file(
    path: Path,
    info: dict,
    playlist_title: str,
    track_num: int,
    settings: dict,
) -> None:
    """Write full ID3 tags for a freshly downloaded file (v2.3)."""
    include = settings.get("include_metadata", _DEFAULTS["include_metadata"])
    video_id = str(info.get("id") or "")
    link = f"https://www.youtube.com/watch?v={video_id}"
    title = _info_field(info, "title", path.stem)
    track = info.get("track")
    if not settings.get("use_title") and track:
        title = str(track)
    artist = _info_field(info, "artist", "")
    uploader = _info_field(info, "uploader", "")
    if settings.get("use_uploader") and uploader:
        artist = uploader
    album = ""
    if settings.get("use_playlist_name"):
        album = playlist_title
    elif info.get("album"):
        album = str(info["album"])

    tags = ID3(path)
    if include.get("title"):
        tags.add(TIT2(encoding=3, text=title))
    if include.get("artist") and artist:
        tags.add(TPE1(encoding=3, text=artist))
    if include.get("album") and album:
        tags.add(TALB(encoding=3, text=album))
    if include.get("track"):
        tags.add(TRCK(encoding=3, text=str(track_num)))
    if include.get("date"):
        upload_date = info.get("upload_date")
        if upload_date:
            try:
                date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
                tags.add(TDRC(encoding=3, text=date))
            except Exception:
                pass
    if include.get("url"):
        tags.add(WOAR(link))
    if include.get("cover"):
        thumbnail = info.get("thumbnail")
        if thumbnail:
            data = _fetch_thumbnail_jpeg(str(thumbnail))
            if data:
                tags.add(
                    APIC(encoding=3, mime="image/jpeg", type=3, desc="Front cover", data=data)
                )
    tags.save(v2_version=3)


# --------------------------------------------------------------------------- #
# Playlist-level sync
# --------------------------------------------------------------------------- #


def _entry_available(entry: dict) -> bool:
    """Mirror of the availability heuristic used for listing entries."""
    if entry.get("channel_id") is None:
        return False
    title = str(entry.get("title") or "").strip()
    for marker in ("[Private video]", "[Deleted video]", "[Video unavailable]", "(Not available)"):
        if title.startswith(marker):
            return False
    return True


def fetch_playlist(url: str, settings: dict) -> dict:
    """Fetch a remote playlist: returns {'title': str, 'entries': [dict]}.

    Each entry carries id/title/channel_id so callers can decide availability.
    """
    opts = {
        "quiet": True,
        "geo_bypass": True,
        "extract_flat": True,
        "skip_download": True,
        "dump_single_json": True,
        "cookiefile": None
        if not settings.get("cookie_file")
        else str(Path(str(settings["cookie_file"])).expanduser()),
        "cookiesfrombrowser": None
        if not settings.get("cookies_from_browser")
        else tuple(str(settings["cookies_from_browser"]).split(":")),
        "extractor_args": settings.get("extractor_args") or {},
        "remote_components": list(settings.get("remote_components") or []),
        "playlistreverse": bool(settings.get("reverse_playlist")),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = []
    for entry in info.get("entries") or []:
        if not entry or not entry.get("id"):
            continue
        entries.append(
            {
                "id": str(entry["id"]),
                "title": str(entry.get("title") or ""),
                "channel_id": entry.get("channel_id"),
                "available": _entry_available(entry),
            }
        )
    return {"title": str(info.get("title") or ""), "entries": entries}


def _strip_track_prefix(stem: str) -> str:
    return _TRACK_PREFIX.sub("", stem)


def sync_playlist(
    folder: Path,
    playlist_title: str,
    entries: List[dict],
    settings: dict,
    log: Optional[Callable[[str], None]] = None,
) -> dict:
    """Download new songs and (re)number/name every song to match `entries`.

    Caller is responsible for cleaning up orphaned/removed songs beforehand.
    Returns {'added': int, 'failed': [str], 'renamed': int}.
    """
    log = log or (lambda m: None)
    result = {"added": 0, "failed": [], "renamed": 0}

    if not ffmpeg_available():
        raise RuntimeError(
            "ffmpeg not found. Please install ffmpeg and ensure it is on your PATH."
        )
    folder.mkdir(parents=True, exist_ok=True)
    local = scan_playlist_folder(folder)

    # 1) Download songs that are available and not present locally.
    jobs = [e for e in entries if e["available"] and e["id"] not in local]
    downloaded: Dict[str, dict] = {}

    def run_download(entry: dict):
        url = f"https://www.youtube.com/watch?v={entry['id']}"
        try:
            info = download_song(url, folder, settings)
            return entry["id"], info, None
        except Exception as e:
            return entry["id"], None, f"{url}: {e}"

    log(f"Downloading {len(jobs)} song(s)...")
    if len(jobs) == 0:
        pass
    elif settings.get("thread_count") and int(settings.get("thread_count")) > 1:
        workers = int(settings["thread_count"])
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run_download, job) for job in jobs]
            for future in concurrent.futures.as_completed(futures):
                video_id, info, error = future.result()
                if error:
                    result["failed"].append(error)
                else:
                    downloaded[video_id] = info
    else:
        for job in jobs:
            log(f"Downloading 'https://www.youtube.com/watch?v={job['id']}'...")
            video_id, info, error = run_download(job)
            if error:
                result["failed"].append(error)
            else:
                downloaded[video_id] = info
    result["added"] = len(downloaded)

    # 2) Decide the final ordering (unavailable + not present songs get no slot).
    placements = []  # (track_num, video_id, body_without_prefix)
    slot = 0
    for entry in entries:
        video_id = entry["id"]
        if video_id in local:
            body = _strip_track_prefix(local[video_id].file_path.stem)
            slot += 1
            placements.append((slot, video_id, body, "held"))
        elif entry["available"] and video_id in downloaded:
            info = downloaded[video_id]
            title = info.get("track") if not settings.get("use_title") and info.get("track") else info.get("title")
            body = f"{sanitize_name(str(title or video_id))}-{video_id}"
            slot += 1
            placements.append((slot, video_id, body, "new"))
        # unavailable and not present -> skip (no local copy to keep)

    # 3) Apply placements: write tags for new files, update TRCK for moves.
    renamed = 0
    for track_num, video_id, body, kind in placements:
        ext = settings.get("audio_codec", "mp3")
        if settings.get("track_num_in_name"):
            final_name = f"{track_num}. {body}.{ext}"
        else:
            final_name = f"{body}.{ext}"
        final_path = folder / final_name

        if kind == "new":
            source = folder / f".ytmirror-{video_id}.{ext}"
            if not source.is_file():
                continue
            try:
                tag_new_file(
                    source, downloaded[video_id], playlist_title, track_num, settings
                )
                if final_path != source:
                    source.rename(final_path)
                else:
                    # Already named correctly only if track prefix off & canonical
                    pass
            except Exception as e:
                result["failed"].append(
                    f"Could not finalise '{video_id}': {e}"
                )
        else:
            current = local[video_id].file_path
            include = settings.get("include_metadata", _DEFAULTS["include_metadata"])
            try:
                if include.get("track"):
                    update_track_tag(current, track_num)
                if final_path != current:
                    current.rename(final_path)
                    renamed += 1
            except Exception as e:
                result["failed"].append(f"Could not reorder '{video_id}': {e}")

    # 4) Remove any leftover temp files.
    for leftover in folder.glob(".ytmirror-*"):
        try:
            leftover.unlink()
        except OSError:
            pass
    result["renamed"] = renamed
    return result
