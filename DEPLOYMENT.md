# Deployment, upgrades, and rollback

These instructions assume the Docker host layout documented in `docker-compose.example.yml`.

## Initial deployment

1. Stop qBittorrent before changing its download bind mount or moving existing data.
2. Create the host directories:

   ```sh
   sudo install -d -o 1000 -g 1000 \
     /data/docker/qbittorrent/config \
     /data/docker/qbittorrent/downloads/INCOMPLETE \
     /data/docker/qbittorrent/downloads/COMPLETE \
     /data/docker/qbittorrent/watch \
     /data/docker/qbittorrent/organizer
   sudo install -d -m 700 /data/docker/qbittorrent/secrets
   ```

3. Copy `organizer.py` and a private `config.json` into `/data/docker/qbittorrent/organizer`.
4. Create `/data/docker/qbittorrent/secrets/qbt_password` with an editor. The file must contain only the qBittorrent Web UI password. Restrict it with `sudo chmod 600`.
5. Start qBittorrent using the example Compose file, adapted to the host as necessary.
6. In qBittorrent, set the incomplete path to `/downloads/INCOMPLETE` and the default save path to `/downloads/COMPLETE`.
7. Configure the completion command:

   ```text
   python3 "/organizer/organizer.py" --config "/organizer/config.json" --hash "%I" --content-path "%F"
   ```

8. Validate before processing a real torrent:

   ```sh
   docker exec qbittorrent python3 /organizer/organizer.py \
     --config /organizer/config.json --check-config
   ```

## Optional recovery timer

Copy the files from `examples/` to `/etc/systemd/system/` on the Docker host, then enable the timer:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now qbit-organizer-scan.timer
systemctl list-timers qbit-organizer-scan.timer
```

The timer runs recovery every 15 minutes. The completion hook remains the primary path; the timer only catches torrents left in staging after a missed or failed hook. The organizer's process lock prevents the timer and hook from mutating qBittorrent simultaneously.

The supplied service runs `docker exec` as UID/GID `1000:1000`, matching the example Compose file's `PUID` and `PGID`. If your container uses different IDs, change `--user` in the service before installing it. Running recovery as root can create destination directories and log files that the qBittorrent process cannot later write.

Inspect timer activity with:

```sh
journalctl -u qbit-organizer-scan.service --since today
```

## Optional event-driven EPUB mirrors

The organizer can request a host-side rclone mirror after it successfully moves an EPUB into a configured subject directory. This keeps cloud access outside the container and never delays or fails torrent organization because of a remote outage.

1. Copy `sync.example.json` to `/data/docker/qbittorrent/organizer/sync.json` and adjust the jobs if necessary.
2. Run `sync_books.py --check-config`, followed by `sync_books.py --dry-run`. Review deletions carefully.
3. Copy `examples/qbit-books-sync.service` and `examples/qbit-books-sync.path` to `/etc/systemd/system/`.
4. Enable `sync_trigger` in the private organizer configuration.
5. Reload systemd and enable the path watcher:

   ```sh
   sudo systemctl daemon-reload
   sudo systemctl enable --now qbit-books-sync.path
   ```

The example watches `/data/docker/qbittorrent/config/qbit-books-sync.trigger` and runs the mirror as host user `mo`. Adjust those values when deploying under a different host layout or account. View results with `journalctl -u qbit-books-sync.service`.

## Upgrade

1. Back up `/data/docker/qbittorrent/config`, the private `config.json`, and the password secret.
2. Download the desired organizer release and review `CHANGELOG.md`.
3. Replace `organizer.py` and the documentation files. Do not overwrite the private `config.json` with `config.example.json`.
4. Compare the new example configuration with the private configuration and add any new settings deliberately.
5. Run `--check-config`.
6. Run `--scan-staging --dry-run` and review the decisions.
7. No container restart is required for an organizer-only update: each hook starts a new Python process and reads the current mounted file.

Upgrade the qBittorrent image separately. Back up `/config`, change the pinned image tag, pull it, and recreate the service only after reviewing qBittorrent's release notes.

## Rollback

1. Download the previous organizer release.
2. Replace `organizer.py` with the previous version.
3. Restore the matching private configuration if the newer release introduced incompatible settings.
4. Run that release's `--check-config` when available.
5. Run `--scan-staging --dry-run`, then `--scan-staging` to recover anything left in `COMPLETE`.

Rolling back the organizer does not require moving torrent data or restoring qBittorrent state. Do not roll back the qBittorrent `/config` directory unless qBittorrent itself was also upgraded and its documented rollback procedure requires it.
