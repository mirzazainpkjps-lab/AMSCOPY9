"""AMS application entrypoint and GitHub auto-deploy webhook."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from flask import jsonify, request

from app import create_app

app = create_app()

BASE_DIR = Path(__file__).resolve().parent
GITHUB_REPO = "https://github.com/mirzazainpkjps-lab/AMSCOPY9.git"
GITHUB_BRANCH = "main"
WEBHOOK_SECRET = (os.environ.get("AMS_WEBHOOK_SECRET") or "").strip()

# PythonAnywhere API settings. PythonAnywhere automatically exposes the
# account API token as API_TOKEN to webapps after an API token is created.
PA_USERNAME = (os.environ.get("PA_USERNAME") or "mirzazain90").strip()
PA_DOMAIN = (os.environ.get("PA_DOMAIN") or "mirzazain90.pythonanywhere.com").strip()
PA_API_TOKEN = (
    os.environ.get("PA_API_TOKEN")
    or os.environ.get("API_TOKEN")
    or ""
).strip()
PA_API_HOST = (os.environ.get("PA_API_HOST") or "www.pythonanywhere.com").strip()

LOG_FILE = BASE_DIR / "deployment.log"
INSTANCE_DIR = BASE_DIR / "instance"
PRESERVE_DIR = BASE_DIR / ".instance_preserve"
DEPLOYMENT_LOCK = threading.Lock()

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("AMS-GitHub")


def run_command(command: list[str], timeout: int = 300):
    logger.info("RUNNING: %s", " ".join(command))
    try:
        result = subprocess.run(
            command,
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        logger.info("EXIT CODE: %s\n%s", result.returncode, result.stdout)
        return result.returncode, result.stdout
    except Exception as exc:
        logger.exception("COMMAND ERROR: %s", exc)
        return 1, str(exc)


def preserve_instance_data() -> bool:
    if not INSTANCE_DIR.exists():
        return True
    try:
        if PRESERVE_DIR.exists():
            shutil.rmtree(PRESERVE_DIR, ignore_errors=True)
        shutil.copytree(INSTANCE_DIR, PRESERVE_DIR, symlinks=True)
        logger.info("Preserved instance data")
        return True
    except Exception:
        logger.exception("Could not preserve instance data")
        return False


def restore_instance_data() -> None:
    if not PRESERVE_DIR.exists():
        return
    try:
        INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
        for source in PRESERVE_DIR.rglob("*"):
            if not source.is_file():
                continue
            relative = source.relative_to(PRESERVE_DIR)
            destination = INSTANCE_DIR / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        logger.info("Restored live instance data")
    except Exception:
        logger.exception("Could not restore instance data")


def reload_pythonanywhere() -> bool:
    """Reload this PythonAnywhere webapp through the official API."""
    if not PA_API_TOKEN:
        logger.error("No PythonAnywhere API token: API_TOKEN/PA_API_TOKEN missing")
        return False

    try:
        import requests
    except ImportError:
        logger.exception("requests is not installed")
        return False

    # Official PythonAnywhere v0 endpoint. PythonAnywhere documents this
    # endpoint as POST /api/v0/user/{username}/webapps/{domain_name}/reload/.
    endpoint = (
        f"https://{PA_API_HOST}/api/v0/user/{PA_USERNAME}/"
        f"webapps/{PA_DOMAIN}/reload/"
    )
    headers = {"Authorization": f"Token {PA_API_TOKEN}"}

    logger.info("Reloading PythonAnywhere webapp via API: %s", endpoint)

    try:
        response = requests.post(endpoint, headers=headers, timeout=60)
    except Exception:
        logger.exception("PythonAnywhere API request failed")
        return False

    logger.info(
        "PythonAnywhere API reload response: status=%s body=%s",
        response.status_code,
        response.text[:2000],
    )

    if 200 <= response.status_code < 300:
        logger.info("PythonAnywhere webapp reload successful")
        return True

    logger.error("PythonAnywhere webapp reload failed: HTTP %s", response.status_code)
    return False


def deploy() -> None:
    if not DEPLOYMENT_LOCK.acquire(blocking=False):
        logger.warning("Deployment already running")
        return

    preserved = False
    try:
        logger.info("========================================")
        logger.info("GITHUB AUTO DEPLOY STARTED")

        preserved = preserve_instance_data()

        code, output = run_command(["git", "remote", "set-url", "origin", GITHUB_REPO])
        if code != 0:
            raise RuntimeError("git remote set-url failed:\n" + output)

        code, output = run_command(["git", "fetch", "--prune", "origin", GITHUB_BRANCH])
        if code != 0:
            raise RuntimeError("git fetch failed:\n" + output)

        code, output = run_command([
            "git", "checkout", "-B", GITHUB_BRANCH,
            f"origin/{GITHUB_BRANCH}",
        ])
        if code != 0:
            raise RuntimeError("git checkout failed:\n" + output)

        code, output = run_command(["git", "reset", "--hard", f"origin/{GITHUB_BRANCH}"])
        if code != 0:
            raise RuntimeError("git reset failed:\n" + output)

        if preserved:
            restore_instance_data()

        requirements = BASE_DIR / "requirements.txt"
        if requirements.exists():
            code, output = run_command([
                sys.executable, "-m", "pip", "install", "--user",
                "-r", str(requirements),
            ], timeout=900)
            if code != 0:
                raise RuntimeError("pip install failed:\n" + output)

        # API reload is the primary reload mechanism.
        if not reload_pythonanywhere():
            logger.warning("API reload failed; trying WSGI touch fallback")
            wsgi_file = Path("/var/www/mirzazain90_pythonanywhere_com_wsgi.py")
            if wsgi_file.exists():
                wsgi_file.touch()
                logger.info("Fallback WSGI reload triggered")
            else:
                logger.error("Fallback WSGI file not found: %s", wsgi_file)

        logger.info("GITHUB AUTO DEPLOY SUCCESS")
        logger.info("========================================")

    except Exception as exc:
        logger.exception("DEPLOYMENT FAILED: %s", exc)
        if preserved:
            restore_instance_data()
    finally:
        DEPLOYMENT_LOCK.release()


def verify_github_signature() -> bool:
    if not WEBHOOK_SECRET:
        logger.error("AMS_WEBHOOK_SECRET is not configured")
        return False

    signature = request.headers.get("X-Hub-Signature-256", "")
    if not signature.startswith("sha256="):
        return False

    expected = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        request.get_data(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature[7:], expected)


@app.route("/git-auto-pull", methods=["GET", "POST"])
def git_auto_pull():
    if request.method == "GET":
        return jsonify({
            "success": True,
            "service": "AMS GitHub Auto Deploy",
            "status": "online",
        }), 200

    event = request.headers.get("X-GitHub-Event", "")
    delivery = request.headers.get("X-GitHub-Delivery", "")

    logger.info("GitHub webhook received: event=%s delivery=%s", event, delivery)

    if not verify_github_signature():
        logger.warning("Invalid GitHub webhook signature")
        return jsonify({"success": False, "message": "Invalid signature"}), 403

    if event != "push":
        return jsonify({"success": True, "message": "Event ignored"}), 200

    payload = request.get_json(silent=True) or {}
    ref = payload.get("ref", "")
    repository = payload.get("repository") or {}
    repository_name = repository.get("full_name", "")

    logger.info("Webhook repository=%s ref=%s", repository_name, ref)

    if repository_name != "mirzazainpkjps-lab/AMSCOPY9":
        return jsonify({"success": True, "message": "Repository ignored"}), 200

    if ref != "refs/heads/main":
        return jsonify({"success": True, "message": "Branch ignored"}), 200

    if DEPLOYMENT_LOCK.locked():
        return jsonify({"success": True, "message": "Deployment already running"}), 202

    threading.Thread(target=deploy, daemon=True).start()

    return jsonify({
        "success": True,
        "message": "Deployment started",
        "repository": repository_name,
        "branch": "main",
        "delivery": delivery,
    }), 202


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
        use_reloader=False,
    )
