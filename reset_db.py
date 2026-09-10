"""
Revfi — Database Reset Utility
Run from the revfi folder:
    python reset_db.py
No environment variables needed.
"""
import os, sys

# Must run from the revfi directory
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from app import app, db, seed_db

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH  = os.path.join(DATA_DIR, "revfi.db")

with app.app_context():
    if os.path.exists(DB_PATH):
        db.session.remove()
        db.engine.dispose()
        os.remove(DB_PATH)
        print("🗑️  Old database removed.")
    else:
        print("ℹ️  No existing database found.")

    db.create_all()
    print("✅  Fresh tables created.")
    seed_db()
    print("✅  Seed data loaded.")
    print("")
    print("Login: shan@revfi.ai / Admin@123")
    print("Done.")
