import os
import subprocess
from pathlib import Path


class GitState:
    """Resolve git directory layout for Docker mounts."""

    def __init__(self, host_dir):
        self.git_mounts = []
        self.container_ws = "/workspace"
        self.container_cmd = ["sleep", "infinity"]
        git_dir = Path(host_dir) / ".git"

        if not git_dir.exists():
            return

        if git_dir.is_dir():
            self.git_mounts = ["-v", f"{host_dir}/.git:/workspace/.git:ro"]
            return

        # Worktree: .git is a file pointing to a gitdir path.
        result = subprocess.run(
            ["git", "-C", host_dir, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return

        real_git = Path(result.stdout.strip()).resolve()

        # Walk up to find the top-level .git directory.
        top_git = real_git
        if real_git.name != ".git":
            for parent in real_git.parents:
                if parent.name == ".git":
                    top_git = parent
                    break

        # Mount workspace at a short path that preserves the depth
        # relative to the git root, so all relative ../.. paths in
        # submodule .git files and git dir configs resolve correctly.
        git_root = top_git.parent
        try:
            ws_rel = Path(host_dir).relative_to(git_root)
            self.container_ws = f"/{ws_rel}"
        except ValueError:
            self.container_ws = host_dir

        # Mount .git at /.git (matches relative depth) and symlink the
        # host absolute path to it so the absolute gitdir reference in
        # the worktree .git file also resolves.
        self.git_mounts = ["-v", f"{top_git}:/.git:ro"]
        self.container_cmd = [
            "bash",
            "-c",
            f"mkdir -p {top_git.parent} && ln -sfn /.git {top_git}"
            " && exec sleep infinity",
        ]


def ensure_worktree(name):
    """Create git worktree if needed and chdir into it."""
    worktree_path = Path.cwd() / "worktrees" / name

    if worktree_path.exists():
        print(f"Worktree already exists: {worktree_path}")
    else:
        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            ["git", "rev-parse", "--verify", name],
            capture_output=True,
            check=False,
        )

        if result.returncode == 0:
            subprocess.run(
                ["git", "worktree", "add", str(worktree_path), name], check=True
            )
        else:
            subprocess.run(
                ["git", "worktree", "add", "-b", name, str(worktree_path)], check=True
            )
        print(f"Created worktree: {worktree_path}")

    os.chdir(worktree_path)

    # Initialize submodules in the worktree
    if Path(".gitmodules").exists():
        subprocess.run(
            ["git", "submodule", "update", "--init", "--recursive"], check=True
        )
