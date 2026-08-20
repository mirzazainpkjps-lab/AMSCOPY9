"""AMS application entrypoint + GitHub auto-pull webhook."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

from flask import jsonify, request

from app import create_app


# ============================================================
# [Ahmed] AMS APPLICATION
# ============================================================

app = create_app()


# ============================================================
# [Ahmed] ENTER YOUR DETAILS HERE
# ============================================================

# 1. ENTER YOUR NEW WEBHOOK TOKEN HERE
#
# IMPORTANT:
# Use a NEW token. Do not use the old token you exposed.
#
WEBHOOK_TOKEN = "PakistanZindabad1947-2026"


# 2. ENTER YOUR PYTHONANYWHERE WSGI FILE PATH HERE
#
# Example:
# /var/www/tempservofbm_pythonanywhere_com_wsgi.py
#
WSGI_FILE = "/var/www/tempservofbm_pythonanywhere_com_wsgi.py"


# ============================================================
# [Ahmed] GITHUB SETTINGS
# ============================================================

GITHUB_REPO = (
    "https://github.com/rehmanahmedca-source/ams99.git"
)

GITHUB_BRANCH = "main"


# ============================================================
# [Ahmed] SYSTEM SETTINGS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

DEPLOYMENT_LOCK = threading.Lock()

LOG_FILE = BASE_DIR / "deployment.log"


# ============================================================
# [Ahmed] LOGGING
# ============================================================

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger("AMS-GitHub")


# ============================================================
# [Ahmed] COMMAND RUNNER
# ============================================================

def run_command(command, timeout=300):

    logger.info(
        "Running: %s",
        " ".join(command),
    )

    try:

        result = subprocess.run(
            command,
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )

        logger.info(
            "Exit code: %s\n%s",
            result.returncode,
            result.stdout,
        )

        return result.returncode, result.stdout

    except Exception as exc:

        logger.exception(
            "Command failed: %s",
            exc,
        )

        return 1, str(exc)


# ============================================================
# [Ahmed] TOKEN VALIDATION
# ============================================================

def valid_token(token):

    if not WEBHOOK_TOKEN:

        logger.error(
            "Webhook token is empty."
        )

        return False

    return token == WEBHOOK_TOKEN


# ============================================================
# [Ahmed] AUTO DEPLOYMENT
# ============================================================

def deploy():

    if not DEPLOYMENT_LOCK.acquire(
        blocking=False
    ):

        logger.warning(
            "Deployment already running."
        )

        return

    try:

        logger.info(
            "========================================"
        )

        logger.info(
            "[Ahmed] GITHUB AUTO DEPLOY STARTED"
        )

        # ----------------------------------------------------
        # STEP 1
        # Fetch latest GitHub code
        # ----------------------------------------------------

        code, output = run_command(
            [
                "git",
                "fetch",
                "--prune",
                "origin",
                GITHUB_BRANCH,
            ]
        )

        if code != 0:

            raise RuntimeError(
                "Git fetch failed:\n" + output
            )

        # ----------------------------------------------------
        # STEP 2
        # Switch to main
        # ----------------------------------------------------

        code, output = run_command(
            [
                "git",
                "checkout",
                "-B",
                GITHUB_BRANCH,
                f"origin/{GITHUB_BRANCH}",
            ]
        )

        if code != 0:

            raise RuntimeError(
                "Git checkout failed:\n" + output
            )

        # ----------------------------------------------------
        # STEP 3
        # Force PythonAnywhere to match GitHub
        # ----------------------------------------------------

        code, output = run_command(
            [
                "git",
                "reset",
                "--hard",
                f"origin/{GITHUB_BRANCH}",
            ]
        )

        if code != 0:

            raise RuntimeError(
                "Git reset failed:\n" + output
            )

        # ----------------------------------------------------
        # STEP 4
        # Install requirements
        # ----------------------------------------------------

        requirements = BASE_DIR / "requirements.txt"

        if requirements.exists():

            logger.info(
                "Installing requirements..."
            )

            code, output = run_command(
                [
                    "python3",
                    "-m",
                    "pip",
                    "install",
                    "--user",
                    "-r",
                    "requirements.txt",
                ],
                timeout=600,
            )

            if code != 0:

                raise RuntimeError(
                    "requirements installation failed:\n"
                    + output
                )

        # ----------------------------------------------------
        # STEP 5
        # Reload PythonAnywhere
        # ----------------------------------------------------

        if WSGI_FILE:

            wsgi_path = Path(
                WSGI_FILE
            ).expanduser()

            if wsgi_path.exists():

                wsgi_path.touch()

                logger.info(
                    "PythonAnywhere WSGI reload triggered."
                )

            else:

                logger.error(
                    "WSGI file NOT FOUND: %s",
                    wsgi_path,
                )

        logger.info(
            "[Ahmed] GITHUB AUTO DEPLOY SUCCESS"
        )

        logger.info(
            "========================================"
        )

    except Exception as exc:

        logger.exception(
            "[Ahmed] DEPLOYMENT FAILED: %s",
            exc,
        )

    finally:

        DEPLOYMENT_LOCK.release()


# ============================================================
# [Ahmed] GITHUB WEBHOOK
# ============================================================

@app.route(
    "/git-auto-pull",
    methods=["GET", "POST"],
)
def git_auto_pull():

    token = request.args.get(
        "token",
        "",
        type=str,
    ).strip()

    # --------------------------------------------------------
    # Verify token
    # --------------------------------------------------------

    if not valid_token(token):

        logger.warning(
            "Unauthorized GitHub deployment request."
        )

        return jsonify(
            {
                "success": False,
                "message": "Unauthorized",
            }
        ), 403

    # --------------------------------------------------------
    # Browser test
    # --------------------------------------------------------

    if request.method == "GET":

        return jsonify(
            {
                "success": True,
                "service": "AMS Git Auto Pull",
                "status": "online",
            }
        ), 200

    # --------------------------------------------------------
    # GitHub event
    # --------------------------------------------------------

    event = request.headers.get(
        "X-GitHub-Event",
        "",
    )

    if event and event != "push":

        return jsonify(
            {
                "success": True,
                "message": "Event ignored",
            }
        ), 200

    # --------------------------------------------------------
    # Check branch
    # --------------------------------------------------------

    payload = request.get_json(
        silent=True
    ) or {}

    ref = payload.get(
        "ref",
        "",
    )

    if ref and ref != "refs/heads/main":

        return jsonify(
            {
                "success": True,
                "message": "Branch ignored",
            }
        ), 200

    # --------------------------------------------------------
    # Prevent duplicate deployment
    # --------------------------------------------------------

    if DEPLOYMENT_LOCK.locked():

        return jsonify(
            {
                "success": True,
                "message": "Deployment already running",
            }
        ), 202

    # --------------------------------------------------------
    # Start deployment
    # --------------------------------------------------------

    thread = threading.Thread(
        target=deploy,
        daemon=True,
    )

    thread.start()

    return jsonify(
        {
            "success": True,
            "message": "Deployment started",
        }
    ), 202


# ============================================================
# [Ahmed] LOCAL FLASK SERVER
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True,
        use_reloader=False,
    )
