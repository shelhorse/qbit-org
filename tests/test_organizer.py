import copy
import importlib.util
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("organizer", ROOT / "organizer.py")
organizer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(organizer)
CONFIG = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))


class ClassificationTests(unittest.TestCase):
    def test_client_accepts_empty_localhost_auth_response(self):
        with mock.patch.object(organizer.QbtClient, "_request", return_value=b""):
            organizer.QbtClient("http://127.0.0.1:8090", "chris", "")

    def test_client_rejects_failed_auth_response(self):
        with (
            mock.patch.object(organizer.QbtClient, "_request", return_value=b"Fails."),
            self.assertRaisesRegex(organizer.OrganizerError, "rejected the login"),
        ):
            organizer.QbtClient("http://127.0.0.1:8090", "chris", "wrong")

    def test_epub_cover_is_not_multiformat(self):
        category, detected = organizer.classify_format(["book.epub", "cover.jpg"], CONFIG)
        self.assertEqual(category, "epub")
        self.assertEqual(detected, {"epub"})

    def test_two_ebook_formats_are_multiformat(self):
        category, _ = organizer.classify_format(["book.epub", "book.mobi"], CONFIG)
        self.assertEqual(category, "multi-format")

    def test_graphic_novel_category_has_no_separator(self):
        category, _ = organizer.classify_format(["book.cbz"], CONFIG)
        self.assertEqual(category, "graphicnovels")

    def test_audio_forces_audiobooks_tag(self):
        tag, reason = organizer.classify_subject(["A fantasy novel"], CONFIG, "audio")
        self.assertEqual(tag, "audiobooks")
        self.assertIn("audio", reason)

    def test_new_special_tags(self):
        examples = {
            "The Complete Works Box Set": "collections",
            "An Introduction to Data Structures": "compsci",
            "Practical Carpentry": "craft",
            "A Guide to Linguistics": "language",
        }
        for title, expected in examples.items():
            with self.subTest(title=title):
                self.assertEqual(organizer.classify_subject([title], CONFIG)[0], expected)

    def test_special_rule_wins_before_general_rule(self):
        tag, reason = organizer.classify_subject(["Coffee: a nonfiction history"], CONFIG)
        self.assertEqual(tag, "coffee")
        self.assertIn("coffee", reason)

    def test_unmatched_title_is_not_guessed(self):
        tag, reason = organizer.classify_subject(["The Ambiguous Book"], CONFIG)
        self.assertEqual(tag, "unclassified")
        self.assertIsNone(reason)

    def test_epub_metadata(self):
        container = b"""<?xml version="1.0"?>
        <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
          <rootfiles><rootfile full-path="content.opf"/></rootfiles>
        </container>"""
        opf = b"""<package xmlns:dc="http://purl.org/dc/elements/1.1/">
          <metadata><dc:title>A Book</dc:title><dc:subject>Travel</dc:subject></metadata>
        </package>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "book.epub"
            with zipfile.ZipFile(path, "w") as book:
                book.writestr("META-INF/container.xml", container)
                book.writestr("content.opf", opf)
            self.assertEqual(organizer.epub_metadata(path), ["A Book", "Travel"])

    def test_destination_layout(self):
        self.assertEqual(organizer.desired_location(CONFIG, "epub", "fiction"), "/downloads/epub/fiction")
        self.assertEqual(organizer.desired_location(CONFIG, "pdf", "fiction"), "/downloads/pdf")

    def test_sync_trigger_is_limited_to_configured_epub_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            trigger = Path(directory) / "sync.trigger"
            config = copy.deepcopy(CONFIG)
            config["sync_trigger"] = {
                "enabled": True,
                "file": str(trigger),
                "tags": ["fiction", "nonfiction"],
            }

            organizer.signal_sync(config, "epub", "fiction")
            self.assertTrue(trigger.exists())

            trigger.unlink()
            organizer.signal_sync(config, "epub", "cooking")
            self.assertFalse(trigger.exists())
            organizer.signal_sync(config, "pdf", "fiction")
            self.assertFalse(trigger.exists())

    def test_apply_plan_sets_category_verify_tag_and_location(self):
        class FakeClient:
            def __init__(self):
                self.posts = []
                self.state = {"category": "", "tags": "", "save_path": "/downloads/COMPLETE"}

            def get_json(self, endpoint, data=None):
                if endpoint.endswith("categories"):
                    return {"epub": {"name": "epub", "savePath": "/downloads/epub"}}
                if endpoint.endswith("tags"):
                    return []
                raise AssertionError(endpoint)

            def post(self, endpoint, data):
                self.posts.append((endpoint, data))
                if endpoint.endswith("setCategory"):
                    self.state["category"] = data["category"]
                elif endpoint.endswith("addTags"):
                    self.state["tags"] = data["tags"]
                elif endpoint.endswith("setLocation"):
                    self.state["save_path"] = data["location"]

            def torrent(self, info_hash):
                return self.state

        with tempfile.TemporaryDirectory() as directory:
            config = copy.deepcopy(CONFIG)
            config["root"] = directory
            location = str(Path(directory) / "epub" / "fiction")
            client = FakeClient()
            organizer.apply_plan(client, "abc123", "epub", "fiction", location, config)
            calls = {endpoint: data for endpoint, data in client.posts}
            self.assertEqual(calls["/api/v2/torrents/setCategory"]["category"], "epub")
            self.assertEqual(calls["/api/v2/torrents/addTags"]["tags"], "fiction,VERIFY")
            self.assertEqual(calls["/api/v2/torrents/setLocation"]["location"], location)
            self.assertTrue(Path(location).is_dir())

    def test_password_file(self):
        with tempfile.TemporaryDirectory() as directory:
            password_file = Path(directory) / "password"
            password_file.write_text("secret-value\n", encoding="utf-8")
            config = {"qbittorrent": {"password_file": str(password_file)}}
            old_direct = os.environ.pop("QBT_PASSWORD", None)
            old_file = os.environ.pop("QBT_PASSWORD_FILE", None)
            try:
                self.assertEqual(organizer.read_password(config), "secret-value")
            finally:
                if old_direct is not None:
                    os.environ["QBT_PASSWORD"] = old_direct
                if old_file is not None:
                    os.environ["QBT_PASSWORD_FILE"] = old_file

    def test_validate_config_rejects_staging_outside_root(self):
        config = copy.deepcopy(CONFIG)
        config["staging_path"] = "/somewhere-else"
        with self.assertRaises(organizer.OrganizerError):
            organizer.validate_config(config)

    def test_path_is_within_staging(self):
        self.assertTrue(organizer.path_is_within("/downloads/COMPLETE/book", "/downloads/COMPLETE"))
        self.assertFalse(organizer.path_is_within("/downloads/COMPLETE-ish/book", "/downloads/COMPLETE"))

    def test_recovery_scan_processes_completed_staging_torrent(self):
        class FakeClient:
            def __init__(self, staging):
                self.state = {
                    "hash": "recover-me",
                    "name": "A fantasy novel",
                    "save_path": staging,
                    "content_path": f"{staging}/book.epub",
                    "category": "",
                    "tags": "",
                }

            def torrents(self, state_filter):
                self.assert_filter = state_filter
                return [self.state]

            def files(self, info_hash):
                return [{"name": "book.epub"}]

            def get_json(self, endpoint, data=None):
                if endpoint.endswith("categories"):
                    return {}
                if endpoint.endswith("tags"):
                    return []
                raise AssertionError(endpoint)

            def post(self, endpoint, data):
                if endpoint.endswith("setCategory"):
                    self.state["category"] = data["category"]
                elif endpoint.endswith("addTags"):
                    self.state["tags"] = data["tags"]
                elif endpoint.endswith("setLocation"):
                    self.state["save_path"] = data["location"]

            def torrent(self, info_hash):
                return self.state

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "COMPLETE"
            staging.mkdir()
            config = copy.deepcopy(CONFIG)
            config["root"] = str(root)
            config["staging_path"] = str(staging)
            config["lock_file"] = str(root / "organizer.lock")
            config["verification_attempts"] = 1
            client = FakeClient(str(staging))
            count, failures = organizer.scan_staging(client, config)
            self.assertEqual((count, failures), (1, 0))
            self.assertEqual(client.assert_filter, "completed")
            self.assertEqual(client.state["save_path"], str(root / "epub" / "fiction"))
            self.assertEqual(client.state["tags"], "fiction,VERIFY")

    def test_organizer_lock_times_out_when_already_held(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {
                "lock_file": str(Path(directory) / "organizer.lock"),
                "lock_timeout_seconds": 0,
            }
            with (
                organizer.organizer_lock(config),
                self.assertRaises(organizer.OrganizerError),
                organizer.organizer_lock(config),
            ):
                self.fail("second lock acquisition unexpectedly succeeded")


if __name__ == "__main__":
    unittest.main()
