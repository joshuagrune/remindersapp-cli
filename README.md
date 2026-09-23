# remindersapp-cli

macOS helper for **Reminders.app** — fast read-only SQLite access for lists and reminders, with AppleScript for writes and a read fallback.

## Requirements

- macOS with Reminders.app
- Python 3.10+
- Automation permission for Terminal/Cursor when using `--execute` writes or the AppleScript read fallback

## Performance

SQLite reads are usually fast and work even if Mail/Reminders automation is unavailable. The AppleScript fallback can be slow. Cache flags remain available:

```bash
python3 apple_reminders.py list --list Reminders --write-cache
python3 apple_reminders.py list --list Reminders --use-cache --cache-max-age 3600
```

Default cache path: `~/.cache/apple-reminders.json`

## Usage

```bash
python3 apple_reminders.py lists
python3 apple_reminders.py list --list Reminders --json
python3 apple_reminders.py add --list Reminders --title "Test" --due 2026-07-05
python3 apple_reminders.py complete --list Reminders --uid <uid>
python3 apple_reminders.py complete --list Reminders --uid <uid> --execute
python3 apple_reminders.py complete --list Reminders --title "Test"
```

Writes are preview-only by default. `list` prints the stable UID used by `complete`.
Exact-title completion remains compatible; multiple matches require
`--all-matches --confirm <printed-code>`.

## License

MIT
