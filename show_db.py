#!/usr/bin/env python3
"""Print RezFi sqlite path, restaurants, and line_item counts.

Uses the stdlib sqlite3 module (do not pip install sqlite3).
Run:  ./venv/bin/python3 show_db.py
"""
from __future__ import print_function

import os
import sqlite3
import sys

APPDIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(APPDIR, "data", "revfi.db")
if not os.path.isfile(DB):
    DB = os.path.join(APPDIR, "data", "aifinance.db")


def main():
    print("python:", sys.executable)
    print("file:", DB)
    if not os.path.isfile(DB):
        print("MISSING", DB)
        return 0
    st = os.stat(DB)
    print("size:", st.st_size, "bytes")
    conn = sqlite3.connect(DB)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        print("tables:", ", ".join(sorted(tables)) or "(none)")
        if "restaurants" in tables:
            rows = conn.execute("SELECT id, name FROM restaurants").fetchall()
            print("restaurants:")
            for rid, name in rows:
                print(" ", rid, name)
        if "line_items" in tables:
            total = conn.execute("SELECT COUNT(*) FROM line_items").fetchone()[0]
            print("line_items:", total)
            for rid, n in conn.execute(
                "SELECT restaurant_id, COUNT(*) FROM line_items GROUP BY restaurant_id"
            ):
                print(" ", rid or "(blank)", n)
        else:
            print("line_items: (no table)")
        if "users" in tables:
            print("users:")
            for un, rid in conn.execute(
                "SELECT username, restaurant_id FROM users"
            ):
                print(" ", un, "restaurant_id=", rid)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as err:
        print("query failed:", err)
        sys.exit(1)
