"""RevFi — Sales CRM. Run: python run.py   Login: admin / admin123"""
import os
from pathlib import Path

os.chdir(Path(__file__).parent)
from app import app  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("PORT") or os.environ.get("REVFI_PORT") or 8092)
    print(f"RevFi Sales CRM  http://127.0.0.1:{port}  (admin / admin123)")
    app.run(host="127.0.0.1", port=port, debug=True)
