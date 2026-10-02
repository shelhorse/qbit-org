#!/usr/bin/env python3
"""Classify a completed qBittorrent book torrent and move it safely via Web API."""

from __future__ import annotations

import argparse
import errno
import http.cookiejar
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Iterable
from contextlib import contextmanager, suppress
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

LOG = logging.getLogger("qbt-book-organizer")
VERSION = "0.3.0"


class OrganizerError(RuntimeError):
    pass


def load_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise OrganizerError(f"Cannot read config {path}: {exc}") from exc

    required = ("root", "format_categories", "classification_rules")
    missing = [key for key in required if key not in config]
    if missing:
        raise OrganizerError(f"Config is missing: {', '.join(missing)}")
    return config


def safe_tag(value: str) -> str:
    value = str(value).strip()
    if not value or "," in value or "\n" in value or "\r" in value:
        raise OrganizerError(f"Unsafe or empty tag: {value!r}")
    return value


def validate_config(config: dict[str, Any], check_paths: bool = False) -> None:
    root = Path(str(config["root"]))
    if not root.is_absolute():
        raise OrganizerError("root must be an absolute path as seen inside qBittorrent")

    build_extension_map(config)
    categories = {safe_component(str(value)) for value in config["format_categories"]}
    categories.add(safe_component(str(config.get("multiformat_category", "multi-format"))))
    unknown = config.get("unknown_format_category")
    if unknown:
        categories.add(safe_component(str(unknown)))

    rule_tags: list[str] = []
    for rule in config["classification_rules"]:
        if "tag" not in rule:
            raise OrganizerError("Every classification rule must contain a tag")
        rule_tags.append(safe_tag(str(rule["tag"])))
        for pattern in rule.get("regex", []):
            try:
                re.compile(str(pattern), flags=re.IGNORECASE)
            except re.error as exc:
                raise OrganizerError(f"Invalid regex for tag {rule['tag']!r}: {exc}") from exc
    duplicates = sorted({tag for tag in rule_tags if rule_tags.count(tag) > 1})
    if duplicates:
        raise OrganizerError(f"Duplicate classification tags: {', '.join(duplicates)}")

    safe_tag(str(config.get("default_tag", "unclassified")))
    for value in config.get("always_tags", []):
        safe_tag(str(value))
    for category, tag in config.get("format_tag_overrides", {}).items():
        if category not in categories:
            raise OrganizerError(f"format_tag_overrides references unknown category {category!r}")
        safe_tag(str(tag))

    qbt = config.get("qbittorrent", {})
    url = str(os.environ.get("QBT_URL", qbt.get("url", "http://127.0.0.1:8080")))
    if urllib.parse.urlparse(url).scheme not in {"http", "https"}:
        raise OrganizerError("qbittorrent.url must begin with http:// or https://")
    if int(qbt.get("timeout_seconds", 30)) < 1:
        raise OrganizerError("qbittorrent.timeout_seconds must be positive")
    if int(config.get("verification_attempts", 10)) < 1:
        raise OrganizerError("verification_attempts must be positive")
    if float(config.get("verification_interval_seconds", 1)) < 0:
        raise OrganizerError("verification_interval_seconds must not be negative")
    if float(config.get("lock_timeout_seconds", 30)) < 0:
        raise OrganizerError("lock_timeout_seconds must not be negative")
    lock_file = Path(str(config.get("lock_file", "/config/qbit-organizer.lock")))
    if not lock_file.is_absolute():
        raise OrganizerError("lock_file must be an absolute path")

    staging = Path(str(config.get("staging_path", root / "COMPLETE")))
    try:
        staging.relative_to(root)
    except ValueError as exc:
        raise OrganizerError("staging_path must be inside root") from exc
    if staging == root:
        raise OrganizerError("staging_path must not be the organizer root")

    if check_paths:
        for label, path in (("root", root), ("staging_path", staging)):
            if not path.is_dir():
                raise OrganizerError(f"{label} does not exist or is not a directory: {path}")
            if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
                raise OrganizerError(f"{label} is not accessible for reading and writing: {path}")


def configure_logging(config: dict[str, Any], verbose: bool = False) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_file = config.get("log_file")
    if log_file:
        try:
            handlers.append(logging.FileHandler(str(log_file), encoding="utf-8"))
        except OSError as exc:
            raise OrganizerError(f"Cannot open log_file {log_file}: {exc}") from exc
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def read_password(config: dict[str, Any]) -> str:
    qbt = config.get("qbittorrent", {})
    direct = os.environ.get("QBT_PASSWORD")
    if direct is not None:
        return direct
    password_file = os.environ.get("QBT_PASSWORD_FILE") or qbt.get("password_file")
    if password_file:
        try:
            return Path(str(password_file)).read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            raise OrganizerError(f"Cannot read qBittorrent password file {password_file}: {exc}") from exc
    return str(qbt.get("password", ""))


def try_file_lock(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        if not handle.read(1):
            handle.seek(0)
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def release_file_lock(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def organizer_lock(config: dict[str, Any]) -> Iterable[None]:
    lock_path = Path(str(config.get("lock_file", "/config/qbit-organizer.lock")))
    timeout = float(config.get("lock_timeout_seconds", 30))
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
    except OSError as exc:
        raise OrganizerError(f"Cannot open lock file {lock_path}: {exc}") from exc

    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                try_file_lock(handle)
                break
            except (BlockingIOError, OSError) as exc:
                if isinstance(exc, OSError) and exc.errno not in {None, errno.EACCES, errno.EAGAIN}:
                    raise OrganizerError(f"Cannot lock {lock_path}: {exc}") from exc
                if time.monotonic() >= deadline:
                    raise OrganizerError(f"Timed out waiting for organizer lock {lock_path}") from exc
                time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        LOG.debug("Acquired organizer lock: %s", lock_path)
        yield
    finally:
        with suppress(OSError):
            release_file_lock(handle)
        handle.close()


def suffix(name: str) -> str:
    return PurePosixPath(name.replace("\\", "/")).suffix.lower().lstrip(".")


def build_extension_map(config: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for category, extensions in config["format_categories"].items():
        for extension in extensions:
            normalized = str(extension).lower().lstrip(".")
            if normalized in result:
                raise OrganizerError(f"Extension .{normalized} appears in both {result[normalized]!r} and {category!r}")
            result[normalized] = category
    return result


def classify_format(file_names: Iterable[str], config: dict[str, Any]) -> tuple[str | None, set[str]]:
    extension_map = build_extension_map(config)
    ignored = {str(item).lower().lstrip(".") for item in config.get("ignored_extensions", [])}
    detected = {
        extension_map[ext]
        for name in file_names
        if (ext := suffix(name)) and ext not in ignored and ext in extension_map
    }

    # Cover art commonly accompanies a single ebook and should not make it multiformat.
    auxiliary = set(config.get("auxiliary_categories_when_book_present", []))
    book_categories = set(config.get("book_categories", ["epub", "azw3", "mobi", "pdf"]))
    if detected & book_categories:
        detected -= auxiliary

    if not detected:
        return config.get("unknown_format_category"), detected
    if len(detected) == 1:
        return next(iter(detected)), detected
    return config.get("multiformat_category", "multiformat"), detected


def normalize_text(parts: Iterable[str]) -> str:
    return " ".join(str(part) for part in parts if part).casefold()


def keyword_matches(text: str, keyword: str) -> bool:
    keyword = keyword.casefold().strip()
    if not keyword:
        return False
    # Word-like keywords get boundaries; punctuation-bearing phrases use substring matching.
    if re.fullmatch(r"[\w -]+", keyword):
        pattern = r"(?<!\w)" + re.escape(keyword).replace(r"\ ", r"[\s._-]+") + r"(?!\w)"
        return re.search(pattern, text, flags=re.IGNORECASE) is not None
    return keyword in text


def classify_subject(
    text_parts: Iterable[str], config: dict[str, Any], format_category: str | None = None
) -> tuple[str, str | None]:
    format_tag = config.get("format_tag_overrides", {}).get(format_category)
    if format_tag:
        return str(format_tag), f"format category {format_category!r}"
    text = normalize_text(text_parts)
    for rule in config["classification_rules"]:
        tag = str(rule["tag"])
        for pattern in rule.get("regex", []):
            if re.search(str(pattern), text, flags=re.IGNORECASE):
                return tag, f"regex {pattern!r}"
        for keyword in rule.get("keywords", []):
            if keyword_matches(text, str(keyword)):
                return tag, f"keyword {keyword!r}"
    return str(config.get("default_tag", "unclassified")), None


def epub_metadata(path: Path) -> list[str]:
    """Return useful OPF metadata without requiring a third-party EPUB package."""
    try:
        with zipfile.ZipFile(path) as book:
            container = ElementTree.fromstring(book.read("META-INF/container.xml"))
            rootfile = container.find(".//{*}rootfile")
            if rootfile is None or not rootfile.get("full-path"):
                return []
            opf = ElementTree.fromstring(book.read(rootfile.get("full-path")))
    except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError):
        return []

    wanted = {"title", "subject", "description", "type"}
    values: list[str] = []
    for element in opf.iter():
        local_name = element.tag.rsplit("}", 1)[-1].casefold()
        if local_name in wanted and element.text:
            values.append(element.text.strip())
    return values


def local_epub_metadata(content_path: str | None) -> list[str]:
    if not content_path:
        return []
    path = Path(content_path)
    candidates: list[Path]
    if path.is_file() and path.suffix.casefold() == ".epub":
        candidates = [path]
    elif path.is_dir():
        candidates = list(path.rglob("*.epub"))
    else:
        candidates = []
    values: list[str] = []
    for candidate in candidates[:100]:
        values.extend(epub_metadata(candidate))
    return values


class QbtClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        response = self._request("POST", "/api/v2/auth/login", {"username": username, "password": password})
        # qBittorrent traditionally returns ``Ok.`` after a successful login.
        # With localhost authentication bypass enabled, qBittorrent 5.2 may
        # instead return HTTP 204 with an empty response body.
        if response.strip() not in {b"", b"Ok."}:
            raise OrganizerError("qBittorrent rejected the login")

    def _request(self, method: str, endpoint: str, data: dict[str, Any] | None = None) -> bytes:
        encoded = urllib.parse.urlencode(data or {}, doseq=True)
        url = self.base_url + endpoint
        body = None
        if method == "GET" and encoded:
            url += "?" + encoded
        elif method == "POST":
            body = encoded.encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={"Referer": self.base_url, "User-Agent": f"qbt-book-organizer/{VERSION}"},
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()
            raise OrganizerError(f"qBittorrent API {endpoint} failed ({exc.code}): {detail}") from exc
        except urllib.error.URLError as exc:
            raise OrganizerError(f"Cannot reach qBittorrent at {self.base_url}: {exc.reason}") from exc

    def get_json(self, endpoint: str, data: dict[str, Any] | None = None) -> Any:
        return json.loads(self._request("GET", endpoint, data).decode("utf-8"))

    def get_text(self, endpoint: str, data: dict[str, Any] | None = None) -> str:
        return self._request("GET", endpoint, data).decode("utf-8", "replace").strip()

    def post(self, endpoint: str, data: dict[str, Any]) -> None:
        self._request("POST", endpoint, data)

    def torrent(self, info_hash: str) -> dict[str, Any]:
        torrents = self.get_json("/api/v2/torrents/info", {"hashes": info_hash})
        for torrent in torrents:
            if torrent.get("hash", "").casefold() == info_hash.casefold():
                return torrent
        raise OrganizerError(f"Torrent hash {info_hash} was not found")

    def torrents(self, state_filter: str = "all") -> list[dict[str, Any]]:
        return self.get_json("/api/v2/torrents/info", {"filter": state_filter})

    def files(self, info_hash: str) -> list[dict[str, Any]]:
        return self.get_json("/api/v2/torrents/files", {"hash": info_hash})


def safe_component(value: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise OrganizerError(f"Unsafe category or tag path component: {value!r}")
    return value


def desired_location(config: dict[str, Any], category: str, tag: str) -> str:
    root = str(config["root"]).rstrip("/\\")
    category = safe_component(category)
    if category == config.get("epub_category", "epub"):
        return f"{root}/{category}/{safe_component(tag)}"
    return f"{root}/{category}"


def ensure_category(client: QbtClient, category: str, save_path: str, update_existing: bool) -> None:
    categories = client.get_json("/api/v2/torrents/categories")
    if category not in categories:
        client.post("/api/v2/torrents/createCategory", {"category": category, "savePath": save_path})
    elif update_existing and categories[category].get("savePath", "").rstrip("/\\") != save_path.rstrip("/\\"):
        client.post("/api/v2/torrents/editCategory", {"category": category, "savePath": save_path})


def split_tags(value: str) -> set[str]:
    return {part.strip() for part in str(value).split(",") if part.strip()}


def verify_plan(
    client: QbtClient,
    info_hash: str,
    category: str,
    desired_tags: Iterable[str],
    location: str,
    attempts: int,
    interval_seconds: float,
) -> None:
    expected_tags = set(desired_tags)
    last: dict[str, Any] = {}
    for attempt in range(attempts):
        last = client.torrent(info_hash)
        category_ok = last.get("category") == category
        tags_ok = expected_tags <= split_tags(str(last.get("tags", "")))
        path_ok = str(last.get("save_path", "")).rstrip("/\\") == location.rstrip("/\\")
        if category_ok and tags_ok and path_ok:
            return
        if attempt + 1 < attempts:
            time.sleep(interval_seconds)
    raise OrganizerError(
        "qBittorrent did not reach the requested state: "
        f"category={last.get('category')!r}, tags={last.get('tags')!r}, "
        f"save_path={last.get('save_path')!r}"
    )


def apply_plan(
    client: QbtClient,
    info_hash: str,
    category: str,
    tag: str,
    location: str,
    config: dict[str, Any],
) -> None:
    try:
        Path(location).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OrganizerError(f"Cannot create destination {location}: {exc}") from exc
    if not os.access(location, os.R_OK | os.W_OK | os.X_OK):
        raise OrganizerError(f"Destination is not accessible for reading and writing: {location}")

    # Manual mode is intentional: category paths are top-level, while EPUB paths include the tag.
    client.post("/api/v2/torrents/setAutoManagement", {"hashes": info_hash, "enable": "false"})
    category_root = str(config["root"]).rstrip("/\\")
    category_path = f"{category_root}/{safe_component(category)}"
    ensure_category(client, category, category_path, bool(config.get("update_existing_category_paths", False)))
    client.post("/api/v2/torrents/setCategory", {"hashes": info_hash, "category": category})

    managed_tags = sorted(
        {str(rule["tag"]) for rule in config["classification_rules"]}
        | {str(value) for value in config.get("format_tag_overrides", {}).values()}
        | {str(config.get("default_tag", "unclassified"))}
    )
    always_tags = [str(value) for value in config.get("always_tags", [])]
    desired_tags = list(dict.fromkeys([tag, *always_tags]))
    existing_tags = set(client.get_json("/api/v2/torrents/tags"))
    missing_tags = [value for value in desired_tags if value not in existing_tags]
    if missing_tags:
        client.post("/api/v2/torrents/createTags", {"tags": ",".join(missing_tags)})
    client.post("/api/v2/torrents/removeTags", {"hashes": info_hash, "tags": ",".join(managed_tags)})
    client.post("/api/v2/torrents/addTags", {"hashes": info_hash, "tags": ",".join(desired_tags)})
    client.post("/api/v2/torrents/setLocation", {"hashes": info_hash, "location": location})
    verify_plan(
        client,
        info_hash,
        category,
        desired_tags,
        location,
        int(config.get("verification_attempts", 10)),
        float(config.get("verification_interval_seconds", 1)),
    )


def signal_sync(config: dict[str, Any], category: str, tag: str) -> None:
    trigger = config.get("sync_trigger", {})
    if not trigger.get("enabled", False):
        return
    if category != config.get("epub_category", "epub") or tag not in trigger.get("tags", []):
        return
    trigger_file = Path(str(trigger.get("file", "")))
    if not trigger_file.is_absolute():
        raise OrganizerError("sync_trigger.file must be an absolute path")
    try:
        trigger_file.parent.mkdir(parents=True, exist_ok=True)
        trigger_file.touch(exist_ok=True)
    except OSError as exc:
        raise OrganizerError(f"Cannot update sync trigger {trigger_file}: {exc}") from exc
    LOG.info("Requested mirror sync: tag=%s trigger=%s", tag, trigger_file)


def path_is_within(value: str, parent: str) -> bool:
    try:
        Path(value).resolve(strict=False).relative_to(Path(parent).resolve(strict=False))
        return True
    except ValueError:
        return False


def process_torrent(
    client: QbtClient,
    torrent: dict[str, Any],
    config: dict[str, Any],
    content_path: str | None = None,
    dry_run: bool = False,
) -> None:
    info_hash = str(torrent.get("hash", ""))
    if not info_hash:
        raise OrganizerError("Torrent response did not include a hash")
    files = client.files(info_hash)
    file_names = [str(item.get("name", "")) for item in files]
    category, detected = classify_format(file_names, config)
    if not category:
        raise OrganizerError("No recognized book format; leaving the torrent unchanged")

    readable_path = content_path or str(torrent.get("content_path", "")) or None
    metadata = [torrent.get("name", ""), *file_names, *local_epub_metadata(readable_path)]
    tag, reason = classify_subject(metadata, config, category)
    location = desired_location(config, category, tag)
    LOG.info(
        "Decision: hash=%s name=%r source=%s formats=%s category=%s tag=%s location=%s%s",
        info_hash,
        torrent.get("name", ""),
        torrent.get("save_path", ""),
        ",".join(sorted(detected)) or "unknown",
        category,
        tag,
        location,
        f" ({reason})" if reason else "",
    )
    if not dry_run:
        with organizer_lock(config):
            apply_plan(client, info_hash, category, tag, location, config)
            signal_sync(config, category, tag)
        LOG.info("Organized successfully: hash=%s location=%s", info_hash, location)


def scan_staging(client: QbtClient, config: dict[str, Any], dry_run: bool = False) -> tuple[int, int]:
    organizer_root = str(config["root"]).rstrip("/\\")
    staging = str(config.get("staging_path", f"{organizer_root}/COMPLETE"))
    candidates = [
        torrent
        for torrent in client.torrents("completed")
        if path_is_within(str(torrent.get("save_path", "")), staging)
        or path_is_within(str(torrent.get("content_path", "")), staging)
    ]
    LOG.info("Recovery scan found %d completed torrent(s) in %s", len(candidates), staging)
    failures = 0
    for torrent in candidates:
        try:
            process_torrent(client, torrent, config, dry_run=dry_run)
        except (OrganizerError, ValueError, re.error) as exc:
            failures += 1
            LOG.error(
                "Recovery failed: hash=%s name=%r error=%s", torrent.get("hash", ""), torrent.get("name", ""), exc
            )
    return len(candidates), failures


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--hash", dest="info_hash", help="Completed torrent info hash (qBittorrent: %%I)")
    mode.add_argument("--scan-staging", action="store_true", help="Process completed torrents still in staging_path")
    mode.add_argument("--check-config", action="store_true", help="Validate rules, paths, credentials, and API access")
    parser.add_argument("--content-path", help="Optional local content path (qBittorrent: %%F) for EPUB metadata")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--dry-run", action="store_true", help="Show the decision without changing qBittorrent")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser.parse_args(argv)


def create_client(config: dict[str, Any]) -> QbtClient:
    qbt = config.get("qbittorrent", {})
    return QbtClient(
        os.environ.get("QBT_URL", qbt.get("url", "http://127.0.0.1:8080")),
        os.environ.get("QBT_USERNAME", qbt.get("username", "admin")),
        read_password(config),
        int(qbt.get("timeout_seconds", 30)),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )
    try:
        config = load_config(args.config)
        configure_logging(config, args.verbose)
        validate_config(config, check_paths=args.check_config or args.scan_staging)
        client = create_client(config)
        if args.check_config:
            version = client.get_text("/api/v2/app/version")
            LOG.info("Configuration is valid; connected to qBittorrent %s", version)
            return 0
        if args.scan_staging:
            count, failures = scan_staging(client, config, dry_run=args.dry_run)
            LOG.info("Recovery scan complete: candidates=%d failures=%d", count, failures)
            return 1 if failures else 0

        torrent = client.torrent(args.info_hash)
        process_torrent(client, torrent, config, args.content_path, args.dry_run)
        return 0
    except (OrganizerError, ValueError, re.error) as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
