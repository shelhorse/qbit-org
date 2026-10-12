#!/usr/bin/env python3
"""Run configured rclone book mirrors safely from the Docker host."""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import logging
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

LOG = logging.getLogger("qbt-book-sync")
VERSION = "0.4.0"


class SyncError(RuntimeError):
    pass


def load_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"Cannot read sync config {path}: {exc}") from exc


def job_sources(job: dict[str, Any]) -> list[dict[str, Any]]:
    configured = job.get("sources")
    if configured is None:
        configured = [job.get("source", "")]
    if not isinstance(configured, list):
        raise SyncError(f"Mirror {job.get('name', '')!r} sources must be a list")
    sources: list[dict[str, Any]] = []
    for value in configured:
        if isinstance(value, str):
            path = value.strip()
            extensions: list[str] = []
        elif isinstance(value, dict):
            path = str(value.get("path", "")).strip()
            configured_extensions = value.get("extensions", [])
            if not isinstance(configured_extensions, list):
                raise SyncError(f"Mirror {job.get('name', '')!r} source extensions must be a list")
            extensions = []
            for extension in configured_extensions:
                normalized = str(extension).strip().casefold()
                if normalized and not normalized.startswith("."):
                    normalized = "." + normalized
                if normalized:
                    extensions.append(normalized)
        else:
            raise SyncError(f"Mirror {job.get('name', '')!r} contains an invalid source")
        if path:
            sources.append({"path": path, "extensions": sorted(set(extensions))})
    if not sources:
        raise SyncError(f"Mirror {job.get('name', '')!r} requires at least one source")
    paths = [source["path"] for source in sources]
    if len(paths) != len(set(paths)):
        raise SyncError(f"Mirror {job.get('name', '')!r} contains duplicate sources")
    return sources


def source_files(source: dict[str, Any]):
    root = Path(source["path"])
    extensions = set(source["extensions"])
    for candidate in root.rglob("*"):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if extensions and candidate.suffix.casefold() not in extensions:
            continue
        yield candidate, candidate.relative_to(root)


@contextmanager
def materialized_source(config: dict[str, Any], job: dict[str, Any]):
    sources = job_sources(job)
    if len(sources) == 1 and not sources[0]["extensions"]:
        yield sources[0]["path"]
        return

    staging_root = Path(str(config.get("staging_root", "/tmp/qbit-books-sync")))
    try:
        staging_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SyncError(f"Cannot create mirror staging directory {staging_root}: {exc}") from exc

    try:
        with tempfile.TemporaryDirectory(prefix="mirror-", dir=staging_root) as directory:
            combined = Path(directory)
            linked = 0
            for source in sources:
                for candidate, relative_path in source_files(source):
                    target = combined / relative_path
                    if target.exists():
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.link(candidate, target)
                    except OSError as exc:
                        if exc.errno == errno.EXDEV:
                            raise SyncError(
                                f"Mirror staging and source must be on the same filesystem: {candidate}"
                            ) from exc
                        raise SyncError(f"Cannot stage mirror file {candidate}: {exc}") from exc
                    linked += 1
            LOG.info("Prepared mirror source: name=%s files=%d path=%s", job["name"], linked, combined)
            yield str(combined)
    except OSError as exc:
        raise SyncError(f"Cannot prepare mirror staging directory: {exc}") from exc


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
        destination = str(job.get("destination", "")).strip()
        if not name or name in seen_names:
            raise SyncError(f"Sync job names must be non-empty and unique: {name!r}")
        seen_names.add(name)
        for configured_source in job_sources(job):
            source = Path(configured_source["path"])
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
    staging_root = Path(str(config.get("staging_root", "/tmp/qbit-books-sync")))
    if not staging_root.is_absolute():
        raise SyncError("staging_root must be an absolute path")
    if not staging_root.parent.is_dir() or not os.access(staging_root.parent, os.R_OK | os.W_OK | os.X_OK):
        raise SyncError(f"staging_root parent is not writable: {staging_root.parent}")
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


def build_command(
    config: dict[str, Any],
    job: dict[str, Any],
    dry_run: bool = False,
    source: str | None = None,
) -> list[str]:
    sources = job_sources(job)
    if source is None:
        if len(sources) != 1 or sources[0]["extensions"]:
            raise SyncError("Multiple or filtered mirror sources must be materialized before syncing")
        source = sources[0]["path"]
    command = [
        str(config.get("rclone", "/usr/bin/rclone")),
        "sync",
        source,
        str(job["destination"]),
        "--max-delete",
        str(int(config.get("max_delete", 20))),
        "--log-level",
        str(config.get("log_level", "INFO")),
    ]
    if dry_run:
        command.append("--dry-run")
    return command


def select_jobs(config: dict[str, Any], job_name: str | None = None) -> list[dict[str, Any]]:
    jobs = list(config["jobs"])
    if job_name is None:
        return jobs
    selected = [job for job in jobs if job["name"] == job_name]
    if not selected:
        raise SyncError(f"Unknown sync job: {job_name!r}")
    return selected


def run_jobs(config: dict[str, Any], dry_run: bool = False, job_name: str | None = None) -> None:
    lock_template = str(config["lock_file"])
    lock_file = Path(lock_template.format(job=job_name or "all"))
    with sync_lock(lock_file):
        for job in select_jobs(config, job_name):
            sources = job_sources(job)
            LOG.info(
                "Starting mirror: name=%s sources=%s destination=%s dry_run=%s",
                job["name"],
                ",".join(source["path"] for source in sources),
                job["destination"],
                dry_run,
            )
            with materialized_source(config, job) as combined_source:
                result = subprocess.run(
                    build_command(config, job, dry_run=dry_run, source=combined_source),
                    check=False,
                )
            if result.returncode:
                raise SyncError(f"Mirror {job['name']!r} failed with exit status {result.returncode}")
            LOG.info("Mirror completed: name=%s", job["name"])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("sync.json"))
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--job", help="Run only the named mirror job")
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
        run_jobs(config, dry_run=args.dry_run, job_name=args.job)
        return 0
    except (SyncError, ValueError, subprocess.SubprocessError) as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
