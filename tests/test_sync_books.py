import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sync_books", ROOT / "sync_books.py")
sync_books = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(sync_books)


class SyncBooksTests(unittest.TestCase):
    def test_build_command_has_bounded_deletes_and_dry_run(self):
        config = {"rclone": "/usr/bin/rclone", "max_delete": 20}
        job = {"source": "/books/fiction", "destination": "gdrive:books/fiction"}
        command = sync_books.build_command(config, job, dry_run=True)
        self.assertEqual(command[:4], ["/usr/bin/rclone", "sync", "/books/fiction", "gdrive:books/fiction"])
        self.assertIn("--max-delete", command)
        self.assertIn("20", command)
        self.assertIn("--dry-run", command)

    def test_materialized_source_filters_multiformat_and_preserves_primary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "epub" / "fiction"
            multiformat = root / "multi-format" / "fiction"
            unrelated = root / "multi-format" / "cooking"
            primary.mkdir(parents=True)
            multiformat.mkdir(parents=True)
            unrelated.mkdir(parents=True)
            (primary / "book.epub").write_text("primary", encoding="utf-8")
            (primary / "cover.jpg").write_text("cover", encoding="utf-8")
            (multiformat / "book.epub").write_text("secondary", encoding="utf-8")
            (multiformat / "another.epub").write_text("included", encoding="utf-8")
            (multiformat / "another.jpg").write_text("included cover", encoding="utf-8")
            (multiformat / "metadata.opf").write_text("included metadata", encoding="utf-8")
            (multiformat / "another.pdf").write_text("excluded", encoding="utf-8")
            (multiformat / "another.mobi").write_text("excluded", encoding="utf-8")
            (unrelated / "cookbook.epub").write_text("wrong subject", encoding="utf-8")
            job = {
                "name": "fiction",
                "sources": [
                    str(primary),
                    {"path": str(multiformat), "extensions": [".epub", ".jpg", ".opf"]},
                ],
                "destination": "gdrive:books/fiction",
            }
            config = {"staging_root": str(root / "staging")}
            with sync_books.materialized_source(config, job) as combined_source:
                combined = Path(combined_source)
                self.assertEqual((combined / "book.epub").read_text(encoding="utf-8"), "primary")
                self.assertTrue((combined / "cover.jpg").exists())
                self.assertTrue((combined / "another.epub").exists())
                self.assertTrue((combined / "another.jpg").exists())
                self.assertTrue((combined / "metadata.opf").exists())
                self.assertFalse((combined / "another.pdf").exists())
                self.assertFalse((combined / "another.mobi").exists())
                self.assertFalse((combined / "cookbook.epub").exists())
                self.assertEqual(
                    (combined / "another.epub").stat().st_ino, (multiformat / "another.epub").stat().st_ino
                )
            self.assertFalse(combined.exists())

    def test_example_config_only_contains_fiction_and_nonfiction(self):
        config = json.loads((ROOT / "sync.example.json").read_text(encoding="utf-8"))
        self.assertEqual([job["name"] for job in config["jobs"]], ["fiction", "nonfiction"])
        self.assertEqual(
            config["jobs"][0]["sources"],
            [
                "/data/docker/qbittorrent/downloads/epub/fiction",
                {
                    "path": "/data/docker/qbittorrent/downloads/multi-format/fiction",
                    "extensions": [
                        ".epub",
                        ".jpg",
                        ".jpeg",
                        ".png",
                        ".webp",
                        ".opf",
                        ".ncx",
                        ".txt",
                        ".json",
                        ".nfo",
                        ".md5",
                        ".sfv",
                    ],
                },
            ],
        )

    def test_select_jobs_returns_only_requested_mirror(self):
        config = {"jobs": [{"name": "fiction"}, {"name": "nonfiction"}]}
        self.assertEqual(sync_books.select_jobs(config, "fiction"), [{"name": "fiction"}])
        with self.assertRaises(sync_books.SyncError):
            sync_books.select_jobs(config, "other")

    def test_config_rejects_missing_source(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {
                "rclone": "/bin/true",
                "lock_file": str(Path(directory) / "lock"),
                "jobs": [{"name": "fiction", "source": str(Path(directory) / "missing"), "destination": "gdrive:x"}],
            }
            with self.assertRaises(sync_books.SyncError):
                sync_books.validate_config(config)


if __name__ == "__main__":
    unittest.main()
