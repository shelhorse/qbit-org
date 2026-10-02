#!/usr/bin/env python3
"""Run configured rclone book mirrors safely from the Docker host."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

LOG = logging.getLogger("qbt-book-sync")
VERSION = "0.3.0"


class SyncError(RuntimeError):
    pass


def load_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"Cannot read sync config {path}: {exc}") from exc


def validate_config(config: dict[str, Any], check_remote: bool = False) -> None:
    rclone = Path(str(config.get("rclone", "/usr/bin/rclone")))
    if not rclone.is_absolute() or not rclone.is_file() or not os.access(rclone, os.X_OK):
        raise SyncError(f"rclone is not executable: {rclone}")
    jobs = config.get("jobs", [])
    if not jobs:
        raise SyncError("At least one sync job is required")
    seen_names: set[str] = set()
    for job in jobs:
        name = str(job.get("name", "")).strip()
        source = Path(str(job.get("source", "")))
        destination = str(job.get("destination", "")).strip()
        if not name or name in seen_names:
            raise SyncError(f"Sync job names must be non-empty and unique: {name!r}")
        seen_names.add(name)
        if not source.is_absolute() or not source.is_dir():
            raise SyncError(f"Sync source is not an existing absolute directory: {source}")
        if ":" not in destination:
            raise SyncError(f"Sync destination must name an rclone remote: {destination!r}")
    max_delete = int(config.get("max_delete", 20))
    if max_delete < 0:
        raise SyncError("max_delete must be zero or greater")
    lock_file = Path(str(config.get("lock_file", "")))
    if not lock_file.is_absolute():
        raise SyncError("lock_file must be an absolute path")
    if check_remote:
        result = subprocess.run([str(rclone), "listremotes"], capture_output=True, check=False, text=True, timeout=30)
        if result.returncode:
            raise SyncError(f"Cannot list rclone remotes: {result.stderr.strip()}")
        remotes = set(result.stdout.splitlines())
        missing = sorted(
            {
                str(job["destination"]).split(":", 1)[0] + ":"
                for job in jobs
                if str(job["destination"]).split(":", 1)[0] + ":" not in remotes
            }
        )
        if missing:
            raise SyncError(f"Unknown rclone remote(s): {', '.join(missing)}")


@contextmanager
def sync_lock(path: Path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
    except OSError as exc:
        raise SyncError(f"Cannot open sync lock {path}: {exc}") from exc
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyncError("Another book mirror is already running") from exc
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def build_command(config: dict[str, Any], job: dict[str, Any], dry_run: bool = False) -> list[str]:
    command = [
        str(config.get("rclone", "/usr/bin/rclone")),
        "sync",
        str(job["source"]),
        str(job["destination"]),
        "--max-delete",
        str(int(config.get("max_delete", 20))),
        "--log-level",
        str(config.get("log_level", "INFO")),
    ]
    if dry_run:
        command.append("--dry-run")
    return command


def run_jobs(config: dict[str, Any], dry_run: bool = False) -> None:
    lock_file = Path(str(config["lock_file"]))
    with sync_lock(lock_file):
        for job in config["jobs"]:
            LOG.info(
                "Starting mirror: name=%s source=%s destination=%s dry_run=%s",
                job["name"],
                job["source"],
                job["destination"],
                dry_run,
            )
            result = subprocess.run(build_command(config, job, dry_run), check=False)
            if result.returncode:
                raise SyncError(f"Mirror {job['name']!r} failed with exit status {result.returncode}")
            LOG.info("Mirror completed: name=%s", job["name"])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("sync.json"))
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(args.config)
        validate_config(config, check_remote=args.check_config)
        if args.check_config:
            LOG.info("Sync configuration is valid")
            return 0
        run_jobs(config, dry_run=args.dry_run)
        return 0
    except (SyncError, ValueError, subprocess.SubprocessError) as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
