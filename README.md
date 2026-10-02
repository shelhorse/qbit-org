# qBittorrent book organizer

This completion hook classifies a torrent by the formats it contains, assigns one subject tag, and asks qBittorrent to move its storage. Because qBittorrent performs the move, the torrent remains seedable.

Examples:

- EPUB + cover image, tagged `fiction` → `/books/epub/fiction`
- MOBI only → `/books/mobi`
- EPUB + MOBI → `/books/multi-format`
- MP3 audiobook → `/books/audio`

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
4. Prefer putting the password in the `QBT_PASSWORD` environment variable. If that is inconvenient for the qBittorrent service, put it in `config.json` and restrict that file to the qBittorrent account.
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
- qBittorrent must have permission to create and move into every destination.
- `root` is not necessarily the host path for a Docker volume. If qBittorrent sees `/downloads/books`, use that even if the host calls it `/mnt/storage/books`.
- Leave `update_existing_category_paths` false if you manage qBittorrent category paths yourself. Set it true only if this script should enforce each category's top-level path.
- Unknown file types are left unchanged unless `unknown_format_category` is set to a category name.
- Re-running the hook is safe: category, managed subject tag, and destination converge on the same result.

## Troubleshooting

Run the command manually with `--verbose --dry-run`. Errors are written to qBittorrent's execution log when it captures hook output. Common causes are an incorrect Web UI URL, Web UI authentication settings, a container path mismatch, or insufficient write permission at the destination.
