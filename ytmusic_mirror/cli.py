"""Command line interface for ytmusic-mirror."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .config import DEFAULT_CONFIG_PATH, Config, write_default_config
from .core import (
    Logger,
    discover_remote_playlists,
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


def _cmd_init(args: argparse.Namespace) -> int:
    config_path = _config_path(args)
    if config_path.exists() and not args.force:
        print(f"Config already exists at {config_path}. Use --force to overwrite.")
        return 1
    path = write_default_config(config_path, music_dir=args.dir)
    print(f"Wrote default config to {path}")
    print("Edit it to set 'channel_url' (your YouTube channel) or 'playlists'.")
    print(f"Playlists will be synced into the master folder set in 'music_dir'.")
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


def _cmd_add(args: argparse.Namespace) -> int:
    config_path = _config_path(args)
    if config_path.exists():
        cfg = Config.load(config_path)
    else:
        print(f"No config at {config_path}; creating one. Add your channel or "
              "more playlists afterwards.", file=sys.stderr)
        write_default_config(config_path)
        cfg = Config.load(config_path)
    added = []
    for url in args.urls:
        if url not in cfg.playlists:
            cfg.playlists.append(url)
            added.append(url)
    cfg.save(config_path)
    print(f"Added {len(added)} playlist URL(s) to {config_path}:")
    for url in added:
        print(f"  - {url}")
    return 0


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

    p_add = sub.add_parser("add", help="Add explicit playlist URLs to the config")
    _add_config_arg(p_add)
    p_add.add_argument("urls", nargs="+", help="YouTube/YouTube Music playlist URL(s)")
    p_add.set_defaults(func=_cmd_add)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
