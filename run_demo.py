"""Demo server runner: serves the AMS app against the seeded demo DB."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["APP_DB_PATH"] = "/tmp/ams_demo.db"
os.environ.setdefault("ALLOW_EMPTY_DB", "1")

from app import create_app

app = create_app({"SECRET_KEY": "demo-preview"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
