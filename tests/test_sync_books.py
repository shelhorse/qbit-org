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

    def test_example_config_only_contains_fiction_and_nonfiction(self):
        config = json.loads((ROOT / "sync.example.json").read_text(encoding="utf-8"))
        self.assertEqual([job["name"] for job in config["jobs"]], ["fiction", "nonfiction"])

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
