#!/usr/bin/env python3
"""Print RevFi sqlite path, tables, and user count.

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


def _cols(conn, table):
    return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}


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
        if "line_items" in tables:
            total = conn.execute("SELECT COUNT(*) FROM line_items").fetchone()[0]
            print("line_items:", total)
        else:
            print("line_items: (no table)")
        if "leads" in tables:
            n = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
            print("leads:", n)
        if "users" in tables:
            print("users:")
            cols = _cols(conn, "users")
            if "email" in cols:
                q = "SELECT name, email, role FROM users"
                for name, email, role in conn.execute(q):
                    print(" ", name, email, role)
            elif "username" in cols:
                extra = ", restaurant_id" if "restaurant_id" in cols else ""
                for row in conn.execute("SELECT username%s FROM users" % extra):
                    print(" ", " ".join(str(x) for x in row))
            else:
                print(" ", "(users table has no email/username column)")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as err:
        print("query failed:", err)
        sys.exit(0)
