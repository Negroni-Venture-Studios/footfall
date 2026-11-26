#!/usr/bin/env python3
"""Create the sign-in account and a dozen tasks.

Reads FOOTFALL_USERNAME and FOOTFALL_PASSWORD from the environment, as Footfall passes
them to a seed step. The password is hashed and never printed. Running it again resets
that account's tasks.

The due dates are fixed, not relative to today, so that the data is the same on every
run. One task falls at 23:58 on the last day of November, just before midnight.
"""

import os
import sys
from contextlib import closing

import serve

TASKS: list[tuple[str, str | None, bool]] = [
    ("Buy oat milk", "2026-12-15", False),
    ("Call the dentist", "2026-12-18", False),
    ("Book train to Cardiff", "2026-12-22", True),
    ("Renew passport", "2027-01-27", False),
    ("Pay council tax", "2026-12-14", True),
    ("Email Mehmet about the İstanbul trip", None, False),
    ("Water the plants", None, False),
    ("Return library books", "2026-12-13", False),
    ("Plan Sunday lunch", "2026-12-20", False),
    ("Order birthday cake", "2026-12-30", True),
    ("Submit expenses", "2026-11-30T23:58", False),
    ("Fix the bike light", None, False),
]


def main() -> int:
    username = os.environ.get("FOOTFALL_USERNAME", "").strip()
    password = os.environ.get("FOOTFALL_PASSWORD", "")
    missing = [
        name
        for name, value in (("FOOTFALL_USERNAME", username), ("FOOTFALL_PASSWORD", password))
        if not value
    ]
    if missing:
        print(f"seed.py needs {' and '.join(missing)} in the environment", file=sys.stderr)
        return 2
    salt = serve.new_salt()
    hashed = serve.hash_password(password, salt)
    with closing(serve.connect()) as db, db:
        db.execute(
            "insert into users (username, salt, hash) values (?, ?, ?) "
            "on conflict (username) do update set salt = excluded.salt, hash = excluded.hash",
            (username, salt, hashed),
        )
        (user_id,) = db.execute("select id from users where username = ?", (username,)).fetchone()
        db.execute("delete from tasks where user_id = ?", (user_id,))
        db.executemany(
            "insert into tasks (user_id, text, due, done) values (?, ?, ?, ?)",
            [(user_id, text, due, int(done)) for text, due, done in TASKS],
        )
    print(f"Seeded {username} with {len(TASKS)} tasks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
