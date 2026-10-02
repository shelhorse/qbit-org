#!/usr/bin/env python3
"""Classify a completed qBittorrent book torrent and move it safely via Web API."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from xml.etree import ElementTree


LOG = logging.getLogger("qbt-book-organizer")


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


def suffix(name: str) -> str:
    return PurePosixPath(name.replace("\\", "/")).suffix.lower().lstrip(".")


def build_extension_map(config: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for category, extensions in config["format_categories"].items():
        for extension in extensions:
            normalized = str(extension).lower().lstrip(".")
            if normalized in result:
                raise OrganizerError(
                    f"Extension .{normalized} appears in both {result[normalized]!r} and {category!r}"
                )
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
        if response.strip() != b"Ok.":
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
            headers={"Referer": self.base_url, "User-Agent": "qbt-book-organizer/1.0"},
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

    def post(self, endpoint: str, data: dict[str, Any]) -> None:
        self._request("POST", endpoint, data)

    def torrent(self, info_hash: str) -> dict[str, Any]:
        torrents = self.get_json("/api/v2/torrents/info", {"hashes": info_hash})
        for torrent in torrents:
            if torrent.get("hash", "").casefold() == info_hash.casefold():
                return torrent
        raise OrganizerError(f"Torrent hash {info_hash} was not found")

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


def apply_plan(
    client: QbtClient,
    info_hash: str,
    category: str,
    tag: str,
    location: str,
    config: dict[str, Any],
) -> None:
    # Manual mode is intentional: category paths are top-level, while EPUB paths include the tag.
    client.post("/api/v2/torrents/setAutoManagement", {"hashes": info_hash, "enable": "false"})
    category_path = f"{str(config['root']).rstrip('/\\')}/{safe_component(category)}"
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hash", dest="info_hash", required=True, help="Completed torrent info hash (qBittorrent: %%I)")
    parser.add_argument("--content-path", help="Optional local content path (qBittorrent: %%F) for EPUB metadata")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--dry-run", action="store_true", help="Show the decision without changing qBittorrent")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        config = load_config(args.config)
        qbt = config.get("qbittorrent", {})
        password = os.environ.get("QBT_PASSWORD", qbt.get("password", ""))
        client = QbtClient(
            os.environ.get("QBT_URL", qbt.get("url", "http://127.0.0.1:8080")),
            os.environ.get("QBT_USERNAME", qbt.get("username", "admin")),
            password,
            int(qbt.get("timeout_seconds", 30)),
        )
        torrent = client.torrent(args.info_hash)
        files = client.files(args.info_hash)
        file_names = [str(item.get("name", "")) for item in files]
        category, detected = classify_format(file_names, config)
        if not category:
            raise OrganizerError("No recognized book format; leaving the torrent unchanged")

        metadata = [torrent.get("name", ""), *file_names, *local_epub_metadata(args.content_path)]
        tag, reason = classify_subject(metadata, config, category)
        location = desired_location(config, category, tag)
        LOG.info(
            "Decision: name=%r formats=%s category=%s tag=%s location=%s%s",
            torrent.get("name", ""),
            ",".join(sorted(detected)) or "unknown",
            category,
            tag,
            location,
            f" ({reason})" if reason else "",
        )
        if not args.dry_run:
            apply_plan(client, args.info_hash, category, tag, location, config)
            LOG.info("qBittorrent updated successfully")
        return 0
    except (OrganizerError, ValueError, re.error) as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
