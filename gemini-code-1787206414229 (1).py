import os
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
        print(f"Created '{CONFIG_FILE}'. Please add your credentials and re-run.")
        sys.exit(1)

    config = {}
    with open(CONFIG_FILE, "r") as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                config[k] = v.strip()
    return config

def setup_gitignore():
    """Ensure system cache, virtual environments, and config files are ignored, while allowing .db files."""
    gitignore_path = ".gitignore"
    needed_entries = [
        CONFIG_FILE,
        ".cache/",
        ".local/",
        ".virtualenvs/",
        ".ipython/",
        ".bashrc",
        ".profile",
        ".vimrc",
        ".lesshst",
        "__pycache__/",
        "*.pyc"
    ]

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

    # Clear lingering Git index locks if present
    index_lock = os.path.join(".git", "index.lock")
    if os.path.exists(index_lock):
        os.remove(index_lock)

    # Initialize Git repository if not initialized
    if not os.path.exists(".git"):
        run_cmd("git init")

    # Set up remote repository URL with credentials
    remote_url = f"https://{username}:{pat}@github.com/{username}/{repo}.git"
    run_cmd(f"git branch -M {branch}")
    run_cmd("git remote remove origin")
    run_cmd(f"git remote add origin {remote_url}")

    # 1. Untrack cache and local environment folders from Git index
    print("Untracking cache, local environment, and system config files...")
    run_cmd("git rm -r --cached .cache .local .virtualenvs .ipython")
    run_cmd("git rm --cached .bashrc .profile .vimrc .lesshst")

    # 2. Stage all valid project files
    run_cmd("git add --all .")

    # 3. Force-stage database files to guarantee tracking
    print("Force-staging database files...")
    run_cmd("git add -f *.db")

    # 4. List exact staged files being committed
    print("\n--- STAGED FILES TO PUSH TO GITHUB ---")
    _, staged_files, _ = run_cmd("git ls-files")

    if staged_files:
        for file_path in staged_files.split("\n"):
            print(f" -> {file_path}")
    else:
        print("No files detected for sync.")
    print("---------------------------------------\n")

    # 5. Commit and push to GitHub
    commit_msg = "Remove system cache while tracking database file"
    run_cmd(f'git commit -m "{commit_msg}"')

    print(f"Uploading files directly to '{branch}' branch...")
    code, out, err = run_cmd(f"git push origin {branch} --force --set-upstream")

    if code == 0:
        print("\nSuccess: Clean app files and database successfully pushed to GitHub!")
    else:
        print(f"\nError transferring files:\n{err}")

if __name__ == "__main__":
    sync_to_github()