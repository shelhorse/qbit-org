# Changelog

All notable changes to this project will be documented here. The project follows semantic versioning.

## 0.1.0 — 2026-10-02

- Classify completed torrents by ebook, document, audio, image, and graphic-novel formats.
- Assign configurable subject tags and add `VERIFY` to every processed torrent.
- Set qBittorrent categories and relocate EPUBs into subject-specific directories.
- Read embedded EPUB metadata without third-party Python packages.
- Support Docker secrets through `QBT_PASSWORD_FILE`.
- Add persistent logging and configuration validation.
- Add destination creation and post-change verification.
- Add recovery scanning for completed torrents left in the staging directory.
- Document the Docker and `INCOMPLETE → COMPLETE → organized` workflow.
