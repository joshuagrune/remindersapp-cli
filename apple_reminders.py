#!/usr/bin/env python3
"""Apple Reminders CLI — fast SQLite reads, AppleScript writes.

Examples:
  python3 apple_reminders.py lists
  python3 apple_reminders.py list --list Reminders --json
  python3 apple_reminders.py add --list Reminders --title "Test" --dry-run
  python3 apple_reminders.py complete --list Reminders --uid <uid> --dry-run
  python3 apple_reminders.py complete --list Reminders --title "Test" --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

LOCAL_TZ = datetime.now().astimezone().tzinfo
REMINDERS_STORES = Path.home() / "Library/Group Containers/group.com.apple.reminders/Container_v1/Stores"
APPLE_EPOCH_UNIX = 978307200
FIELD_SEP = "\x1f"
RECORD_SEP = "\x1e"
MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


class RemindersError(RuntimeError):
    pass


@dataclass
class ReminderRow:
    list_name: str
    name: str
    due: str | None
    notes: str
    completed: bool
    priority: int | None = None
    uid: str = ""


def run_osascript(source: str) -> str:
    proc = subprocess.run(
        ["osascript", "-e", source],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "osascript failed").strip()
        raise RemindersError(detail)
    return proc.stdout.strip()


def applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def confirmation_code(action: str, list_name: str, rows: list[ReminderRow]) -> str:
    target_fingerprint = [
        {"uid": row.uid, "name": row.name, "due": row.due}
        for row in sorted(rows, key=lambda row: (row.uid, row.due or "", row.name))
    ]
    raw = json.dumps(
        {"action": action, "list": list_name, "targets": target_fingerprint},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def applescript_set_date(var_name: str, dt: datetime) -> str:
    month = MONTH_NAMES[dt.month - 1]
    return f"""
set {var_name} to current date
set year of {var_name} to {dt.year}
set month of {var_name} to {month}
set day of {var_name} to {dt.day}
set hours of {var_name} to {dt.hour}
set minutes of {var_name} to {dt.minute}
set seconds of {var_name} to {dt.second}
""".strip()


def parse_iso_datetime(value: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=LOCAL_TZ)
        except ValueError:
            continue
    raise RemindersError(f"Invalid datetime {value!r}")


def list_names() -> list[str]:
    try:
        return list_names_sqlite()
    except (RemindersError, sqlite3.Error, OSError):
        return list_names_applescript()


def reminder_databases() -> list[Path]:
    paths = sorted(REMINDERS_STORES.glob("Data-*.sqlite"))
    if not paths:
        raise RemindersError("Reminders SQLite stores not found")
    return paths


def open_reminder_db(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


def list_names_sqlite() -> list[str]:
    names: list[str] = []
    for path in reminder_databases():
        with open_reminder_db(path) as conn:
            names.extend(
                name for (name,) in conn.execute(
                    "SELECT ZNAME FROM ZREMCDBASELIST WHERE ZNAME IS NOT NULL "
                    "AND COALESCE(ZMARKEDFORDELETION, 0) = 0 "
                    "AND COALESCE(ZISGROUP, 0) = 0 ORDER BY ZDADISPLAYORDER, Z_PK"
                )
            )
    return list(dict.fromkeys(names))


def list_names_applescript() -> list[str]:
    raw = run_osascript(
        """
tell application "Reminders"
  set output to ""
  repeat with L in lists
    set output to output & (name of L) & linefeed
  end repeat
  return output
end tell
"""
    )
    return [line.strip() for line in raw.splitlines() if line.strip()]


def fetch_reminders(*, list_filter: str | None, include_completed: bool) -> list[ReminderRow]:
    try:
        return fetch_reminders_sqlite(list_filter=list_filter, include_completed=include_completed)
    except (RemindersError, sqlite3.Error, OSError):
        return fetch_reminders_applescript(list_filter=list_filter, include_completed=include_completed)


def fetch_reminders_sqlite(*, list_filter: str | None, include_completed: bool) -> list[ReminderRow]:
    rows: list[ReminderRow] = []
    for path in reminder_databases():
        with open_reminder_db(path) as conn:
            conn.row_factory = sqlite3.Row
            sql = """
                SELECT l.ZNAME AS list_name, r.ZTITLE AS name, r.ZDUEDATE AS due,
                       r.ZNOTES AS notes, r.ZCOMPLETED AS completed,
                       r.ZPRIORITY AS priority, r.ZDACALENDARITEMUNIQUEIDENTIFIER AS uid
                FROM ZREMCDREMINDER r
                JOIN ZREMCDBASELIST l ON l.Z_PK = r.ZLIST
                WHERE COALESCE(r.ZMARKEDFORDELETION, 0) = 0
                  AND COALESCE(l.ZMARKEDFORDELETION, 0) = 0
                  AND COALESCE(l.ZISGROUP, 0) = 0
            """
            params: list[str] = []
            if list_filter:
                sql += " AND l.ZNAME = ?"
                params.append(list_filter)
            if not include_completed:
                sql += " AND COALESCE(r.ZCOMPLETED, 0) = 0"
            for record in conn.execute(sql, params):
                due = record["due"]
                rows.append(ReminderRow(
                    list_name=record["list_name"],
                    name=record["name"] or "",
                    due=datetime.fromtimestamp(APPLE_EPOCH_UNIX + due).strftime("%Y-%m-%dT%H:%M:%S") if due is not None else None,
                    notes=record["notes"] or "",
                    completed=bool(record["completed"]),
                    priority=record["priority"],
                    uid=record["uid"] or "",
                ))
    rows.sort(key=lambda r: (r.list_name.lower(), r.due or "", r.name.lower()))
    return rows


def fetch_reminders_applescript(*, list_filter: str | None, include_completed: bool) -> list[ReminderRow]:
    if list_filter:
        list_literals = applescript_string(list_filter)
        list_block = f"set targetLists to {{list {list_literals}}}"
    else:
        list_block = "set targetLists to every list"

    completed_filter = "true" if include_completed else "false"
    script = f"""
set fieldSep to ASCII character 31
set recordSep to ASCII character 30
set output to ""
on pad2(n)
  if n < 10 then return "0" & n
  return n as string
end pad2

tell application "Reminders"
  {list_block}
  repeat with L in targetLists
    set listName to name of L
    set rs to (reminders of L whose completed is {completed_filter})
    repeat with r in rs
      set dueStr to ""
      try
        set d to due date of r
        if d is not missing value then
          set dueStr to (year of d as string) & "-" & (my pad2(month of d as integer)) & "-" & (my pad2(day of d)) & "T" & (my pad2(hours of d)) & ":" & (my pad2(minutes of d)) & ":" & (my pad2(seconds of d))
        end if
      end try
      set noteText to ""
      try
        set noteText to body of r
      end try
      if noteText is missing value then set noteText to ""
      set prio to 0
      try
        set prio to priority of r
      end try
      set output to output & listName & fieldSep & (name of r) & fieldSep & dueStr & fieldSep & noteText & fieldSep & (completed of r as string) & fieldSep & prio & fieldSep & (id of r) & recordSep
    end repeat
  end repeat
end tell
return output
"""
    raw = run_osascript(script)
    rows: list[ReminderRow] = []
    if not raw:
        return rows
    for record in raw.split(RECORD_SEP):
        if not record.strip():
            continue
        parts = record.split(FIELD_SEP)
        if len(parts) < 7:
            continue
        list_name, name, due, notes, completed_s, prio_s, uid = parts[:7]
        rows.append(
            ReminderRow(
                list_name=list_name,
                name=name,
                due=due or None,
                notes=notes,
                completed=completed_s.lower() == "true",
                priority=int(prio_s) if prio_s.isdigit() else None,
                uid=uid,
            )
        )
    rows.sort(key=lambda r: (r.list_name.lower(), r.due or "", r.name.lower()))
    return rows


def write_cache(path: Path, rows: list[ReminderRow]) -> None:
    payload = {
        "fetched_at": datetime.now(tz=LOCAL_TZ).isoformat(),
        "reminders": [asdict(r) for r in rows],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_cache(path: Path, max_age_seconds: int) -> list[ReminderRow] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(payload["fetched_at"])
        age = (datetime.now(tz=LOCAL_TZ) - fetched).total_seconds()
        if age > max_age_seconds:
            return None
        return [ReminderRow(**row) for row in payload.get("reminders", [])]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def cmd_lists(args: argparse.Namespace) -> int:
    names = list_names()
    if args.json:
        print(json.dumps({"lists": names}, ensure_ascii=False, indent=2))
        return 0
    for name in names:
        print(name)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    cache_path = Path(args.cache).expanduser()
    rows: list[ReminderRow] | None = None
    if args.use_cache:
        rows = read_cache(cache_path, args.cache_max_age)
        if rows is not None:
            print(f"(cache hit: {cache_path}, max_age={args.cache_max_age}s)", file=sys.stderr)

    if rows is None:
        rows = fetch_reminders(list_filter=args.list, include_completed=args.completed)
        if args.write_cache:
            write_cache(cache_path, rows)

    if args.json:
        print(
            json.dumps(
                {
                    "source": "Reminders.app",
                    "performance_note": "SQLite reads are fast; AppleScript is used if the local store is unavailable.",
                    "list_filter": args.list,
                    "include_completed": args.completed,
                    "reminders": [asdict(r) for r in rows],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not rows:
        print("(keine Reminders)")
        return 0

    current_list: str | None = None
    for row in rows:
        if row.list_name != current_list:
            current_list = row.list_name
            print(f"=== {current_list} ===")
        due = f" | due {row.due}" if row.due else ""
        state = " [done]" if row.completed else ""
        print(f"  - {row.name}{due}{state} | uid={row.uid}")
    print(f"\n({len(rows)} reminders via Reminders.app)")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    preview = {"list": args.list, "title": args.title, "notes": args.notes or None, "due": args.due}
    if args.json:
        print(json.dumps({"dry_run": not args.execute, "reminder": preview}, ensure_ascii=False, indent=2))
    else:
        print("Reminder Create Preview")
        for key, value in preview.items():
            print(f"  {key}: {value}")

    if not args.execute:
        if not args.json:
            print("\nDry run only. Pass --execute after explicit user confirmation.")
        return 0

    due_block = ""
    if args.due:
        due_dt = parse_iso_datetime(args.due)
        due_block = f"""
{applescript_set_date("dueD", due_dt)}
set due date of newReminder to dueD
"""
    script = f"""
tell application "Reminders"
  tell list {applescript_string(args.list)}
    set newReminder to make new reminder with properties {{name:{applescript_string(args.title)}, body:{applescript_string(args.notes or "")}}}
    {due_block}
    return id of newReminder
  end tell
end tell
"""
    uid = run_osascript(script)
    print(f"created reminder uid={uid}")
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    rows = fetch_reminders(list_filter=args.list, include_completed=False)
    matches = [row for row in rows if row.uid == args.uid] if args.uid else [
        row for row in rows if row.name == args.title
    ]
    if not matches:
        raise RemindersError("No matching incomplete reminder found")
    if any(not row.uid for row in matches):
        raise RemindersError("A matching reminder has no UID; refusing completion")

    code = confirmation_code("complete", args.list, matches)
    preview = {
        "list": args.list,
        "selector": {"uid": args.uid, "title": args.title},
        "targets": [asdict(row) for row in matches],
        "confirmation": code if len(matches) > 1 else None,
    }
    if args.json:
        print(json.dumps({"dry_run": not args.execute, "complete": preview}, ensure_ascii=False, indent=2))
    else:
        print("Reminder Complete Preview")
        print(f"  list: {args.list}")
        print(f"  selector: {'uid=' + args.uid if args.uid else 'title=' + args.title}")
        print(f"  targets: {len(matches)}")
        for row in matches:
            print(f"    - uid={row.uid} | due={row.due or '-'} | {row.name}")
        if len(matches) > 1:
            print(f"  confirmation: {code}")

    if not args.execute:
        if not args.json:
            print("\nDry run only. Pass --execute after explicit user confirmation.")
            if len(matches) > 1:
                print(f"Multiple matches also require --all-matches --confirm {code}.")
        return 0

    if len(matches) > 1:
        if not args.all_matches:
            raise RemindersError("Multiple title matches; pass --all-matches after reviewing the preview")
        if args.confirm != code:
            raise RemindersError(f"Multiple title matches require --confirm {code}")
    elif args.confirm:
        raise RemindersError("--confirm is only used for multiple matches")

    uid_literals = ", ".join(applescript_string(row.uid) for row in matches)
    script = f"""
set targetUIDs to {{{uid_literals}}}
tell application "Reminders"
  tell list {applescript_string(args.list)}
    set resolvedTargets to {{}}
    repeat with targetUID in targetUIDs
      set uidMatches to (every reminder whose id is (targetUID as string) and completed is false)
      if (count of uidMatches) is not 1 then error "UID did not resolve to exactly one incomplete reminder"
      set end of resolvedTargets to item 1 of uidMatches
    end repeat
    repeat with r in resolvedTargets
      set completed of r to true
    end repeat
    return count of resolvedTargets
  end tell
end tell
"""
    count = run_osascript(script)
    print(f"completed {count} reminder(s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_lists = sub.add_parser("lists", help="List Reminders lists")
    p_lists.add_argument("--json", action="store_true")
    p_lists.set_defaults(func=cmd_lists)

    p_list = sub.add_parser("list", help="List reminders (slow)")
    p_list.add_argument("--list", help="Filter to one list name")
    p_list.add_argument("--completed", action="store_true", help="Include completed reminders")
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("--cache", default="~/.cache/apple-reminders.json")
    p_list.add_argument("--use-cache", action="store_true")
    p_list.add_argument("--write-cache", action="store_true")
    p_list.add_argument("--cache-max-age", type=int, default=3600)
    p_list.set_defaults(func=cmd_list)

    p_add = sub.add_parser("add", help="Create a reminder")
    p_add.add_argument("--list", required=True)
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--notes", default="")
    p_add.add_argument("--due", help="YYYY-MM-DD or YYYY-MM-DDTHH:MM")
    p_add_mode = p_add.add_mutually_exclusive_group()
    p_add_mode.add_argument("--execute", action="store_true")
    p_add_mode.add_argument("--dry-run", action="store_false", dest="execute")
    p_add.add_argument("--json", action="store_true")
    p_add.set_defaults(func=cmd_add, execute=False)

    p_done = sub.add_parser("complete", help="Complete reminder by UID (title lookup is compatibility mode)")
    p_done.add_argument("--list", required=True)
    p_done_target = p_done.add_mutually_exclusive_group(required=True)
    p_done_target.add_argument("--uid", help="Preferred: exact reminder UID from list JSON/output")
    p_done_target.add_argument("--title", help="Compatibility lookup by exact title")
    p_done.add_argument("--all-matches", action="store_true", help="Allow all reviewed title matches")
    p_done.add_argument("--confirm", help="Confirmation code printed for multiple matches")
    p_done_mode = p_done.add_mutually_exclusive_group()
    p_done_mode.add_argument("--execute", action="store_true", help="Complete the reviewed target(s)")
    p_done_mode.add_argument("--dry-run", action="store_false", dest="execute", help="Preview only (default)")
    p_done.add_argument("--json", action="store_true")
    p_done.set_defaults(func=cmd_complete, execute=False)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except RemindersError as exc:
        print(f"apple_reminders.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
