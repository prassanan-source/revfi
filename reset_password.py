"""
Revfi — Password Reset Utility
Run from the revfi folder:
    python reset_password.py
No environment variables needed.
"""
import os, sys
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from app import app, db, User
from werkzeug.security import generate_password_hash

with app.app_context():
    print("\n=== Revfi Password Reset ===\n")

    # List all users
    users = User.query.order_by(User.role.desc(), User.name).all()
    print("Current users:")
    for u in users:
        print(f"  [{u.id}] {u.name} <{u.email}> — {u.role}{'  ← INACTIVE' if not u.active else ''}")

    print()
    email = input("Enter email to reset (or press Enter for shan@revfi.ai): ").strip()
    if not email:
        email = "shan@revfi.ai"

    user = User.query.filter_by(email=email).first()
    if not user:
        print(f"\n❌ No user found with email: {email}")
        sys.exit(1)

    print(f"\nResetting password for: {user.name} ({user.role})")
    new_pass = input("New password (or press Enter for 'Admin@123'): ").strip()
    if not new_pass:
        new_pass = "Admin@123"

    user.password = generate_password_hash(new_pass)
    user.active = True   # reactivate if disabled
    db.session.commit()

    print(f"\n✅ Password reset for {user.email}")
    print(f"   New password: {new_pass}")
    print(f"   Login at:     http://localhost:5050/login\n")
