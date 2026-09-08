"""Command line interface for ytmusic-mirror."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

from . import __version__
from .config import DEFAULT_CONFIG_PATH, Config, write_default_config
from .core import (
    Logger,
    discover_remote_playlists,
    playlist_id_from_url,
    pretty_report,
    sync,
)


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-c",
        "--config",
        default=None,
        help=f"Config file path (default: {DEFAULT_CONFIG_PATH})",
    )


def _add_dir_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-d",
        "--dir",
        default=None,
        metavar="DIR",
        help="Master folder where playlists are synced (overrides config 'music_dir')",
    )


def _config_path(args: argparse.Namespace) -> Path:
    return Path(args.config).expanduser() if args.config else DEFAULT_CONFIG_PATH


def _load_config(args: argparse.Namespace) -> Config:
    cfg = Config.load(_config_path(args))
    if getattr(args, "dir", None):
        cfg.music_dir = Path(args.dir).expanduser().resolve()
    return cfg


def _load_or_create_config(args: argparse.Namespace) -> Tuple[Config, Path]:
    """Load the config, creating a default one first if it does not exist."""
    config_path = _config_path(args)
    if not config_path.exists():
        print(f"No config at {config_path}; creating a default one first.",
              file=sys.stderr)
        write_default_config(config_path)
    return Config.load(config_path), config_path


# --------------------------------------------------------------------------- #
# Normalisation helpers
# --------------------------------------------------------------------------- #


def _normalize_channel(value: str) -> str:
    """Accept @handle, full channel URL or a bare youtube.com channel link."""
    candidate = value.strip()
    if not candidate:
        raise ValueError("Empty channel value.")
    if playlist_id_from_url(candidate):
        raise ValueError(
            "That looks like a playlist URL. Playlists are added with "
            "`ytmusic-mirror add <url>`, not as a channel."
        )
    if "youtube.com" in candidate or "youtu.be" in candidate:
        candidate = candidate if urlparse(candidate).scheme else f"https://{candidate}"
        return candidate.rstrip("/")
    if candidate.startswith("@"):
        return f"https://www.youtube.com/{candidate}"
    raise ValueError(
        "Channel must be a YouTube URL (e.g. https://www.youtube.com/@handle) "
        "or an @handle."
    )


def _normalize_playlist(value: str) -> str:
    if not playlist_id_from_url(value):
        raise ValueError(
            f"Not a playlist URL (missing ?list=): {value}"
        )
    return value.strip()


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def _cmd_init(args: argparse.Namespace) -> int:
    config_path = _config_path(args)
    if config_path.exists() and not args.force:
        print(f"Config already exists at {config_path}. Use --force to overwrite.")
        return 1
    path = write_default_config(config_path, music_dir=args.dir)
    print(f"Wrote default config to {path}")
    print("Now tell it what to mirror, for example:")
    print(f"  ytmusic-mirror channel https://www.youtube.com/@YourHandle")
    print(f"  ytmusic-mirror add <playlist-url> [more-playlist-urls...]")
    print(f"Playlists will be synced into the master folder set in 'music_dir'.")
    return 0


def _cmd_channel(args: argparse.Namespace) -> int:
    cfg, config_path = _load_or_create_config(args)
    if args.clear:
        cfg.channel_url = ""
        cfg.save(config_path)
        print("Channel cleared.")
        return 0
    if not args.url:
        print(cfg.channel_url if cfg.channel_url else "No channel set.")
        return 0
    try:
        normalized = _normalize_channel(args.url)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    cfg.channel_url = normalized
    cfg.save(config_path)
    print(f"Channel set to: {normalized}")
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    cfg, config_path = _load_or_create_config(args)
    known = {playlist_id_from_url(u) for u in cfg.playlists if playlist_id_from_url(u)}
    added = []
    errors = []
    for raw in args.urls:
        try:
            url = _normalize_playlist(raw)
        except ValueError as e:
            errors.append(str(e))
            continue
        if playlist_id_from_url(url) in known:
            errors.append(f"Already added: {url}")
            continue
        cfg.playlists.append(url)
        known.add(playlist_id_from_url(url))
        added.append(url)
    cfg.save(config_path)
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if added:
        print(f"Added {len(added)} playlist URL(s):")
        for url in added:
            print(f"  - {url}")
    if not added and errors:
        return 1
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    cfg, config_path = _load_or_create_config(args)
    if args.channel:
        cfg.channel_url = ""
        cfg.save(config_path)
        print("Channel cleared.")
        return 0
    if not args.urls:
        print("Nothing to remove. Use `remove <url>` or `remove --channel`.",
              file=sys.stderr)
        return 1
    ids = {playlist_id_from_url(u) for u in args.urls}
    kept = [u for u in cfg.playlists if playlist_id_from_url(u) not in ids]
    removed = len(cfg.playlists) - len(kept)
    cfg.playlists = kept
    cfg.save(config_path)
    if removed:
        print(f"Removed {removed} playlist(s).")
    else:
        print("No matching playlists found in config.")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    config_path = _config_path(args)
    if not config_path.exists():
        print(f"No config yet at {config_path}.")
        print(f"Run `ytmusic-mirror init --dir <folder>` to create one.")
        return 0
    cfg = Config.load(config_path)
    print(f"Config file : {config_path}")
    print(f"Master folder: {cfg.music_dir}")
    print(f"Channel      : {cfg.channel_url or '(not set)'}")
    print(f"Playlists    : {len(cfg.playlists)} added explicit playlist(s)")
    for url in cfg.playlists:
        print(f"  - {url}")
    print(f"Archive dir  : {cfg.effective_archive_dir}")
    print(f"deleted_playlist_policy: {cfg.deleted_playlist_policy}")
    print(f"orphan_policy: {cfg.orphan_policy}")
    if cfg.cookies_from_browser or cfg.cookie_file:
        print(f"cookies      : browser={cfg.cookies_from_browser or '-'} file={cfg.cookie_file or '-'}")
    return 0


def _cmd_remote(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    try:
        remote = discover_remote_playlists(cfg)
    except Exception as e:
        print(f"Discovery failed: {e}", file=sys.stderr)
        return 1
    if not remote:
        print("No remote playlists found.")
        return 0
    if args.json:
        print(json.dumps([{"id": r.id, "title": r.title, "url": r.url} for r in remote], indent=2))
    else:
        for r in remote:
            print(f"{r.id}\t{r.title}\t{r.url}")
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    log = Logger(quiet=args.nolog)
    if args.dry_run:
        print("DRY RUN - no files will be downloaded, moved, renamed or deleted.\n")
    try:
        report = sync(cfg, dry_run=args.dry_run, log=log)
    except KeyboardInterrupt:
        print("\nInterrupted. Nothing was corrupted: just run the same command "
              "again and it will resume where it left off.", file=sys.stderr)
        return 130
    print(pretty_report(report))
    return 1 if report.errors else 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ytmusic-mirror",
        description="Mirror public YouTube Music playlists to local MP3 folders "
                    "(Windows and Linux).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Write a default config file")
    _add_config_arg(p_init)
    _add_dir_arg(p_init)
    p_init.add_argument("--force", action="store_true", help="Overwrite an existing config")
    p_init.set_defaults(func=_cmd_init)

    p_channel = sub.add_parser(
        "channel",
        help="Set/show/clear the channel whose playlists are mirrored",
    )
    _add_config_arg(p_channel)
    p_channel.add_argument("url", nargs="?", help="Channel URL or @handle")
    p_channel.add_argument("--clear", action="store_true", help="Clear the configured channel")
    p_channel.set_defaults(func=_cmd_channel)

    p_add = sub.add_parser("add", help="Add explicit playlist URLs to the config")
    _add_config_arg(p_add)
    p_add.add_argument("urls", nargs="+", help="YouTube/YouTube Music playlist URL(s)")
    p_add.set_defaults(func=_cmd_add)

    p_remove = sub.add_parser("remove", help="Remove playlists from the config")
    _add_config_arg(p_remove)
    p_remove.add_argument("urls", nargs="*", help="Playlist URL(s) to remove")
    p_remove.add_argument("--channel", action="store_true", help="Clear the configured channel")
    p_remove.set_defaults(func=_cmd_remove)

    p_status = sub.add_parser("status", help="Show the current configuration")
    _add_config_arg(p_status)
    p_status.set_defaults(func=_cmd_status)

    p_remote = sub.add_parser("remote", help="List remote playlists that would be synced")
    _add_config_arg(p_remote)
    p_remote.add_argument("--json", action="store_true", help="Print as JSON")
    p_remote.set_defaults(func=_cmd_remote)

    p_sync = sub.add_parser("sync", help="Sync remote playlists into the master folder")
    _add_config_arg(p_sync)
    _add_dir_arg(p_sync)
    p_sync.add_argument("--dry-run", action="store_true", help="Only print what would change")
    p_sync.add_argument(
        "--nolog", action="store_true",
        help="Suppress progress/info output (errors are still shown)",
    )
    p_sync.set_defaults(func=_cmd_sync)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
