# Changelog

All notable changes to this project will be documented here. The project follows semantic versioning.

## 0.3.1 — 2026-10-02

- Give fiction and nonfiction independent trigger files and systemd instances so only the corresponding mirror runs.

## 0.3.0 — 2026-10-02

- Add an optional post-organization trigger restricted to configured EPUB subject tags.
- Add a config-driven host-side rclone mirror command with dry-run, validation, locking, and deletion limits.
- Add systemd path/service examples for event-driven fiction and nonfiction mirrors.

## 0.2.2 — 2026-10-02

- Accept qBittorrent 5.2's successful empty authentication response when localhost authentication bypass is enabled.
- Run the example recovery service as the qBittorrent UID/GID to preserve writable directory and log ownership.

## 0.2.1 — 2026-10-02

- Add Ruff linting and formatting checks.
- Add branch-coverage reporting without enforcing a minimum percentage.
- Run quality checks automatically in GitHub Actions.
- Update GitHub Actions to Node 24-compatible releases.

## 0.2.0 — 2026-10-02

- Serialize completion hooks and recovery scans with a crash-safe operating-system file lock.
- Add a pinned Docker Compose example.
- Add an optional systemd recovery timer.
- Add documented deployment, upgrade, and rollback procedures.

## 0.1.1 — 2026-10-02

- Restore compatibility with Python 3.9 and 3.11 by avoiding backslashes inside f-string expressions.

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
