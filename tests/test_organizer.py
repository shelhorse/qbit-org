import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("organizer", ROOT / "organizer.py")
organizer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(organizer)
CONFIG = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))


class ClassificationTests(unittest.TestCase):
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
        container = b'''<?xml version="1.0"?>
        <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
          <rootfiles><rootfile full-path="content.opf"/></rootfiles>
        </container>'''
        opf = b'''<package xmlns:dc="http://purl.org/dc/elements/1.1/">
          <metadata><dc:title>A Book</dc:title><dc:subject>Travel</dc:subject></metadata>
        </package>'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "book.epub"
            with zipfile.ZipFile(path, "w") as book:
                book.writestr("META-INF/container.xml", container)
                book.writestr("content.opf", opf)
            self.assertEqual(organizer.epub_metadata(path), ["A Book", "Travel"])

    def test_destination_layout(self):
        self.assertEqual(organizer.desired_location(CONFIG, "epub", "fiction"), "/books/epub/fiction")
        self.assertEqual(organizer.desired_location(CONFIG, "pdf", "fiction"), "/books/pdf")

    def test_apply_plan_sets_category_verify_tag_and_location(self):
        class FakeClient:
            def __init__(self):
                self.posts = []

            def get_json(self, endpoint, data=None):
                if endpoint.endswith("categories"):
                    return {"epub": {"name": "epub", "savePath": "/books/epub"}}
                if endpoint.endswith("tags"):
                    return []
                raise AssertionError(endpoint)

            def post(self, endpoint, data):
                self.posts.append((endpoint, data))

        client = FakeClient()
        organizer.apply_plan(client, "abc123", "epub", "fiction", "/books/epub/fiction", CONFIG)
        calls = {endpoint: data for endpoint, data in client.posts}
        self.assertEqual(calls["/api/v2/torrents/setCategory"]["category"], "epub")
        self.assertEqual(calls["/api/v2/torrents/addTags"]["tags"], "fiction,VERIFY")
        self.assertEqual(calls["/api/v2/torrents/setLocation"]["location"], "/books/epub/fiction")


if __name__ == "__main__":
    unittest.main()
