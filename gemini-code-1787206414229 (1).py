import os
import shutil
import subprocess
import sys

# ==========================================
# CONFIGURATION LOAD
# ==========================================
CONFIG_FILE = "config.txt"

def load_config():
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f:
            f.write("GITHUB_USERNAME=your_username\n")
            f.write("GITHUB_PAT=your_personal_access_token\n")
            f.write("REPO_NAME=ams99\n")
            f.write("BRANCH_NAME=main\n")
        print(f"Created '{CONFIG_FILE}'. Please add credentials and re-run.")
        sys.exit(1)

    config = {}
    with open(CONFIG_FILE, "r") as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                config[k] = v.strip()
    return config

def setup_gitignore():
    """Ignore only config credentials so PAT is not exposed."""
    gitignore_path = ".gitignore"
    needed_entries = [CONFIG_FILE, "__pycache__/", "*.pyc"]

    existing = []
    if os.path.exists(gitignore_path):
        with open(gitignore_path, "r") as f:
            existing = [line.strip() for line in f.readlines()]

    with open(gitignore_path, "a") as f:
        for entry in needed_entries:
            if entry not in existing:
                f.write(f"\n{entry}")

def run_cmd(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()

def sync_to_github():
    config = load_config()
    username = config.get("GITHUB_USERNAME")
    pat = config.get("GITHUB_PAT")
    repo = config.get("REPO_NAME")
    branch = config.get("BRANCH_NAME", "main")

    setup_gitignore()

    # 1. Clear Git index lock if present
    index_lock = os.path.join(".git", "index.lock")
    if os.path.exists(index_lock):
        os.remove(index_lock)

    # 2. Remove any nested hidden .git folders inside ams99 or subdirectories
    # This prevents Git from treating ams99 as an empty submodule
    for root, dirs, _ in os.walk("."):
        if root != "." and ".git" in dirs:
            nested_git = os.path.join(root, ".git")
            shutil.rmtree(nested_git)

    # 3. Initialize Git if not initialized
    if not os.path.exists(".git"):
        run_cmd("git init")

    # 4. Set Remote URL
    remote_url = f"https://{username}:{pat}@github.com/{username}/{repo}.git"
    run_cmd(f"git branch -M {branch}")
    run_cmd("git remote remove origin")
    run_cmd(f"git remote add origin {remote_url}")

    # 5. FULL RESET OF GIT CACHED INDEX
    # Unstages everything from Git memory without deleting local files
    print("Resetting Git tracking index...")
    run_cmd("git rm -r --cached .")

    # 6. FORCE-ADD EVERYTHING IN THE DIRECTORY
    print("Staging ALL files and subdirectories (including ams99 and .db files)...")
    run_cmd("git add -A .")

    # 7. List exact staged files queued for upload
    print("\n--- ALL STAGED FILES TO PUSH TO GITHUB ---")
    _, staged_files, _ = run_cmd("git ls-files")

    if staged_files:
        for file_path in staged_files.split("\n"):
            print(f" -> {file_path}")
    else:
        print("No files detected for sync.")
    print("-------------------------------------------\n")

    # 8. Commit and Force Push
    run_cmd('git commit -m "Full transfer of all data, project files, and database"')

    print(f"Uploading all files directly to '{branch}' branch...")
    code, out, err = run_cmd(f"git push origin {branch} --force --set-upstream")

    if code == 0:
        print("\nSuccess: Everything in the directory has been pushed to GitHub!")
    else:
        print(f"\nError transferring files:\n{err}")

if __name__ == "__main__":
    sync_to_github()