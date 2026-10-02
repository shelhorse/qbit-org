# qBittorrent book organizer

This completion hook classifies a torrent by the formats it contains, assigns one subject tag, and asks qBittorrent to move its storage. Because qBittorrent performs the move, the torrent remains seedable.

Examples:

- EPUB + cover image, tagged `fiction` → `/downloads/epub/fiction`
- MOBI only → `/downloads/mobi`
- EPUB + MOBI → `/downloads/multi-format`
- MP3 audiobook → `/downloads/audio`

The script explicitly sets the qBittorrent category as well as the location. It preserves unrelated existing tags, replaces only the managed subject tags, and adds `VERIFY` to every processed torrent. Automatic Torrent Management is turned off for the processed torrent so qBittorrent does not replace `/epub/<tag>` with the category's top-level path later.

## Requirements

- qBittorrent 4.1 or newer with Web UI enabled
- Python 3.9 or newer
- The script must run somewhere that can reach qBittorrent's Web UI
- The configured root must be a path as qBittorrent sees it (important with Docker)

No Python packages are required.

## Set up

1. Copy this entire folder to a permanent location visible to qBittorrent.
2. Copy `config.example.json` to `config.json`.
3. Edit `config.json`:
   - Change `root` to your real root as seen by qBittorrent.
   - Set the Web UI URL and username.
   - Customize extensions, tags, and keyword rules.
4. Prefer a Docker secret or `QBT_PASSWORD_FILE`. `QBT_PASSWORD` is also supported. Putting the password directly in `config.json` is supported only as a fallback.
5. On Linux/macOS, make `organizer.py` executable.
6. Test one completed torrent from a terminal first:

   ```sh
   QBT_PASSWORD='your-password' python3 /path/to/qbt-book-organizer/organizer.py \
     --config /path/to/qbt-book-organizer/config.json \
     --hash THE_TORRENT_HASH --content-path '/path/to/download' --dry-run
   ```

7. In qBittorrent, open **Tools → Options → Downloads**, enable **Run external program on torrent completion**, and enter one of these commands.

Linux/macOS:

```text
python3 "/path/to/qbt-book-organizer/organizer.py" --config "/path/to/qbt-book-organizer/config.json" --hash "%I" --content-path "%F"
```

Windows:

```text
py "C:\path\to\qbt-book-organizer\organizer.py" --config "C:\path\to\qbt-book-organizer\config.json" --hash "%I" --content-path "%F"
```

`%I` is qBittorrent's torrent-hash placeholder and `%F` is its content-path placeholder.

## Docker deployment

The official `ghcr.io/qbittorrent/docker-qbittorrent-nox` image already includes Python 3. Mount the organizer read-only alongside the existing qBittorrent volumes:

The same configuration is available as [docker-compose.example.yml](docker-compose.example.yml). See [DEPLOYMENT.md](DEPLOYMENT.md) for initial deployment, a recovery timer, upgrades, and rollback.

```yaml
services:
  qbittorrent:
    # Deliberately pinned; review and update this tag when upgrading qBittorrent.
    image: ghcr.io/qbittorrent/docker-qbittorrent-nox:5.2.3-1
    container_name: qbittorrent
    restart: unless-stopped
    stop_grace_period: 30m
    read_only: false

    environment:
      - QBT_WEBUI_PORT=8090
      - QBT_TORRENTING_PORT=50286
      - TZ=America/Chicago
      - PUID=1000
      - PGID=1000

    ports:
      - "8090:8090"
      - "50286:50286"
      - "50286:50286/udp"

    volumes:
      - /data/docker/qbittorrent/config:/config
      - /data/docker/qbittorrent/downloads:/downloads
      - /data/docker/qbittorrent/watch:/watch
      - /data/docker/qbittorrent/organizer:/organizer:ro

    secrets:
      - qbt_password

    tmpfs:
      - /tmp:noexec,nosuid,size=100M

secrets:
  qbt_password:
    file: /data/docker/qbittorrent/secrets/qbt_password
```

Copy `organizer.py` and a private `config.json` into `/data/docker/qbittorrent/organizer` on the host. `config.json` is intentionally excluded by `.gitignore` because it may contain the Web UI password. In this layout its essential settings are:

```json
{
  "qbittorrent": {
    "url": "http://127.0.0.1:8090",
    "username": "admin",
    "password_file": "/run/secrets/qbt_password"
  },
  "root": "/downloads",
  "staging_path": "/downloads/COMPLETE",
  "log_file": "/config/qbit-organizer.log",
  "lock_file": "/config/qbit-organizer.lock",
  "lock_timeout_seconds": 30
}
```

Create the password file outside the organizer directory and restrict it to the administrator:

```sh
sudo install -d -m 700 /data/docker/qbittorrent/secrets
sudoedit /data/docker/qbittorrent/secrets/qbt_password
sudo chmod 600 /data/docker/qbittorrent/secrets/qbt_password
```

Enter only the Web UI password in that file, without quotes.

Configure qBittorrent's completion command inside the Web UI:

```text
python3 "/organizer/organizer.py" --config "/organizer/config.json" --hash "%I" --content-path "%F"
```

All configured paths must use the container's view. For example, `/data/docker/qbittorrent/downloads` on the host is `/downloads` inside qBittorrent.

## Staging completed downloads

Use `COMPLETE` as a staging directory, not as the organizer root. For a clean three-stage workflow, configure qBittorrent with:

```text
Keep incomplete torrents in: /downloads/INCOMPLETE
Default save path:           /downloads/COMPLETE
Organizer root:              /downloads
```

The resulting flow is:

```text
/downloads/INCOMPLETE
        ↓ download completes
/downloads/COMPLETE
        ↓ completion hook classifies and moves it
/downloads/epub/fiction
/downloads/pdf
/downloads/audio
/downloads/graphicnovels
/downloads/multi-format
```

The completion hook does not use `staging_path` to calculate destinations. It inspects the completed torrent wherever it currently resides and calculates the final location from `root`. `staging_path` is used only by recovery scans. If classification or relocation fails, the torrent remains conspicuously in `COMPLETE` for manual review. Re-running the hook for the same torrent is safe.

Create the staging directories and ensure UID/GID 1000 can write to the download tree before enabling the hook. Pre-creating the format and EPUB tag directories is also a useful way to detect permission mistakes during setup.

## Validation and recovery

Validate the complete configuration, filesystem permissions, credentials, and qBittorrent API connection before enabling the hook:

```sh
docker exec qbittorrent python3 /organizer/organizer.py \
  --config /organizer/config.json --check-config
```

If a completion hook is missed because of a restart or temporary failure, scan every completed torrent still under `staging_path`:

```sh
docker exec qbittorrent python3 /organizer/organizer.py \
  --config /organizer/config.json --scan-staging
```

Preview the recovery decisions without changing anything:

```sh
docker exec qbittorrent python3 /organizer/organizer.py \
  --config /organizer/config.json --scan-staging --dry-run
```

The recovery command is safe to schedule from the Docker host with cron or a systemd timer. It considers only completed torrents whose save or content path is inside `staging_path`. A failure processing one torrent does not prevent the remaining candidates from being attempted.

Before moving a torrent, the organizer creates the destination directory. It then verifies that qBittorrent reports the requested category, all requested tags, and the final save path. The retry count and interval are controlled by `verification_attempts` and `verification_interval_seconds`.

## Concurrent runs

Every mutation acquires the operating-system lock configured by `lock_file`. This serializes simultaneous completion hooks and prevents a scheduled recovery scan from racing a hook. The operating system releases the lock automatically if a process exits or crashes; the presence of the lock file itself does not mean the organizer is stuck.

`lock_timeout_seconds` controls how long a process waits for another organizer run. A timeout leaves the torrent in `COMPLETE`, where a later recovery scan can retry it. Dry runs never acquire the mutation lock.

## Event-driven Google Drive mirrors

Cloud mirroring is deliberately separate from the completion hook so a slow or unavailable remote cannot block qBittorrent. After a successfully verified EPUB move, the organizer updates the trigger file for that subject. Independent systemd path instances run only the corresponding `sync_books.py` job on the Docker host.

The example configuration mirrors only `epub/fiction` and `epub/nonfiction`. No other EPUB directory is passed to rclone. Copy `sync.example.json` to `sync.json`, validate it, and inspect a dry run before enabling the path unit:

```sh
python3 /data/docker/qbittorrent/organizer/sync_books.py \
  --config /data/docker/qbittorrent/organizer/sync.json --check-config
python3 /data/docker/qbittorrent/organizer/sync_books.py \
  --config /data/docker/qbittorrent/organizer/sync.json --dry-run
```

The mirror uses `rclone sync`, so remote files missing locally are deleted. `max_delete` limits the number of deletions in one run. Set `sync_trigger.enabled` to `true` in the organizer configuration only after reviewing the dry run and installing the service and path units.

## Logging

Console output is always available to qBittorrent's external-program log. When `log_file` is configured, the same records are appended there. Each decision includes the torrent hash, name, original path, detected formats, category, tag, and destination. Credentials are never logged.

In the documented Docker layout, the default log is persistent at `/config/qbit-organizer.log` inside the container and under `/data/docker/qbittorrent/config` on the host.

## Classification behavior

Format classification uses qBittorrent's file list. Cover images are ignored when an ebook format is present, so an EPUB with a JPG cover stays in `epub`. Two real book formats result in `multi-format`. Comic archives use the `graphicnovels` category.

Subject classification examines the torrent name, contained filenames, and—when `%F` is locally readable—the title, subject, description, and type stored inside EPUB files. Rules are evaluated in order, so special subjects should remain above `fiction` and `nonfiction`. Audio-format torrents use the `audiobooks` subject tag. The supplied special tags are `audiobooks`, `collections`, `compsci`, `craft`, `language`, `coffee`, `cooking`, `travel`, and `health`.

No local-only classifier can reliably infer fiction versus nonfiction from every title. A torrent that matches no rule receives `unclassified`; this is deliberate, to avoid confidently filing a book in the wrong place. Add author, publisher, series, or tracker naming conventions to the keyword rules as you observe them. Each rule may also contain Python regular expressions, for example:

```json
{
  "tag": "fiction",
  "regex": ["\\b(SF|sci[ ._-]?fi)\\b"],
  "keywords": ["novel", "fantasy"]
}
```

For non-EPUB formats the tag is still added, but the location remains the top-level format directory, matching the requested layout.

## Operational notes

- Run the dry-run command before enabling the hook.
- Run `--check-config` after changing rules, paths, credentials, or container mounts.
- qBittorrent must have permission to create and move into every destination.
- `root` is not necessarily the host path for a Docker volume. If qBittorrent sees `/downloads`, use that even if the host calls it `/data/docker/qbittorrent/downloads`.
- Leave `update_existing_category_paths` false if you manage qBittorrent category paths yourself. Set it true only if this script should enforce each category's top-level path.
- Unknown file types are left unchanged unless `unknown_format_category` is set to a category name.
- Re-running the hook is safe: category, managed subject tag, and destination converge on the same result.

## Troubleshooting

Run the command manually with `--verbose --dry-run`. Errors are written to qBittorrent's execution log when it captures hook output. Common causes are an incorrect Web UI URL, Web UI authentication settings, a container path mismatch, or insufficient write permission at the destination.

Check the persistent log with:

```sh
docker exec qbittorrent tail -n 100 /config/qbit-organizer.log
```

## Releases

The organizer follows semantic versioning. Review [CHANGELOG.md](CHANGELOG.md) before upgrading. The Docker example pins qBittorrent rather than tracking `latest`; update the image tag deliberately after reviewing qBittorrent's release notes and backing up `/config`.

## Development checks

Runtime operation remains dependency-free. Contributors can install the pinned development-only tools with:

```sh
python3 -m pip install -r requirements-dev.txt
```

Run the same checks used by GitHub Actions:

```sh
ruff check .
ruff format --check .
coverage run -m unittest discover -s tests -v
coverage report
```

Ruff detects likely errors and enforces consistent formatting. Coverage reports statement and branch coverage with missing line numbers. Coverage is informational: the project does not reject a change solely because of a percentage threshold.
