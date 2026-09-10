"""
Passenger WSGI entry point for TMDHosting/cPanel shared hosting.
Upload this alongside app.py in the same directory.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

# Set secret key from file (same pattern as Raya finance app)
secret_path = os.path.join(os.path.dirname(__file__), "data", ".secret_key")
if os.path.exists(secret_path):
    with open(secret_path) as f:
        os.environ["FLASK_SECRET_KEY"] = f.read().strip()

from app import app as application
