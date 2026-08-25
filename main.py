"""AMS application entrypoint + GitHub auto-pull webhook."""

from __future__ import annotations

import logging
import os
import shutil
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
# Prefer the AMS_WEBHOOK_TOKEN environment variable. The literal below is
# only a fallback for existing deployments — a token committed to the
# repository is public, so set the environment variable and rotate it.
WEBHOOK_TOKEN = (
    os.environ.get("AMS_WEBHOOK_TOKEN")
    or "PakistanZindabad1947-2026"
)


# 2. ENTER YOUR PYTHONANYWHERE WSGI FILE PATH HERE
#
# Example:
# /var/www/tempservofbm_pythonanywhere_com_wsgi.py
#
WSGI_FILE = "/var/www/mirzazain90_pythonanywhere_com_wsgi.py"


# ============================================================
# [Ahmed] GITHUB SETTINGS
# ============================================================

GITHUB_REPO = (
    "https://github.com/mirzazainpkjps-lab/AMSCOPY9"
)

GITHUB_BRANCH = "main"


# ============================================================
# [Ahmed] SYSTEM SETTINGS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

DEPLOYMENT_LOCK = threading.Lock()

LOG_FILE = BASE_DIR / "deployment.log"

# The instance directory holds the LIVE application data (the SQLite
# database plus its -wal/-shm files, the health snapshot, logs and the
# secret key).  It must never be overwritten by a code deployment.
INSTANCE_DIR = BASE_DIR / "instance"
INSTANCE_PRESERVE_DIR = BASE_DIR / ".instance_preserve"


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
# [Ahmed] LIVE DATA PROTECTION (instance directory)
# ============================================================
#
# The GitHub auto-deploy below runs ``git checkout`` + ``git reset --hard``
# on top of the live deployment.  The live SQLite database (and its
# -wal/-shm files) live inside the repository checkout under ``instance/``,
# so a blind reset overwrites the LIVE database with whatever copy was last
# committed to GitHub.  That is exactly how saved sales (and other data)
# "disappear" after a push, then "reappear" when a newer snapshot is
# committed — while rows saved in between are lost.
#
# These helpers snapshot the instance directory BEFORE the reset and put the
# live files back AFTER it, so a deployment updates code only and can never
# roll back application data.


def preserve_instance_data():
    """Copy the live instance directory aside before the git reset."""
    if not INSTANCE_DIR.exists():
        logger.info("No instance directory to preserve.")
        return None
    try:
        if INSTANCE_PRESERVE_DIR.exists():
            shutil.rmtree(
                INSTANCE_PRESERVE_DIR,
                ignore_errors=True,
            )
        shutil.copytree(
            INSTANCE_DIR,
            INSTANCE_PRESERVE_DIR,
            symlinks=True,
            ignore=shutil.ignore_patterns(
                ".instance_preserve",
            ),
        )
        logger.info(
            "Preserved live instance data to %s before git reset.",
            INSTANCE_PRESERVE_DIR,
        )
        return True
    except Exception as exc:
        # Never block a deploy because the safety copy failed, but make it
        # loud: without the restore step the reset would clobber live data.
        logger.exception(
            "WARNING: could NOT preserve instance data: %s",
            exc,
        )
        return False


def restore_instance_data(preserved):
    """Put the live instance files back after the git reset.

    Every file that existed before the deployment is restored from the
    preserved copy, so the reset can change code but not data.  Files that
    are new in the commit (e.g. a fresh config) are left in place.
    """
    if not preserved:
        logger.warning(
            "Skipping instance restore because the preserve step did not "
            "complete. The git reset may have overwritten live data — "
            "verify instance/ahmed_cement_v44_fresh.db immediately."
        )
        return
    try:
        INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
        restored = 0
        for src in sorted(INSTANCE_PRESERVE_DIR.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(INSTANCE_PRESERVE_DIR)
            dst = INSTANCE_DIR / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            restored += 1
        logger.info(
            "Restored %d live instance file(s) after git reset.",
            restored,
        )
    except Exception as exc:
        logger.exception(
            "WARNING: instance restore failed: %s",
            exc,
        )


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

    preserved = False

    try:

        logger.info(
            "========================================"
        )

        logger.info(
            "[Ahmed] GITHUB AUTO DEPLOY STARTED"
        )

        # ----------------------------------------------------
        # STEP 0
        # Preserve the LIVE database and instance data so the
        # git checkout/reset below can never roll back live
        # application data to the last committed snapshot.
        # ----------------------------------------------------

        preserved = preserve_instance_data()

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

        # Restore the live instance files on every path — success or
        # failure — so a failed deploy midway through the git reset
        # cannot leave the live database in the committed state.
        restore_instance_data(preserved)

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

    # Werkzeug's debugger allows arbitrary code execution for anyone who can
    # reach the port, and this server binds 0.0.0.0 (the whole LAN).  Keep it
    # off unless AMS_DEBUG=1 is set explicitly.
    debug_mode = (os.environ.get("AMS_DEBUG") or "").strip() == "1"

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000") or "5000"),
        debug=debug_mode,
        use_reloader=False,
    )
