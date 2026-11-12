from unittest.mock import patch

from sux.git import GitState


def make_git_dir(path):
    """A directory that looks like a git dir with hooks and config."""
    (path / "hooks").mkdir(parents=True)
    (path / "HEAD").write_text("ref: refs/heads/main\n")
    (path / "config").write_text("[core]\n")


class TestGitState:
    def test_no_git_dir(self, tmp_path):
        git = GitState(str(tmp_path))
        assert git.git_mounts == []
        assert git.container_ws == "/workspace"
        assert not hasattr(git, "container_cmd")

    def test_regular_git_dir(self, tmp_path):
        top = tmp_path / ".git"
        make_git_dir(top)
        make_git_dir(top / "modules" / "group" / "sub")
        # a branch called `config` and a ref namespace called `hooks`
        (top / "refs" / "heads" / "hooks").mkdir(parents=True)
        (top / "refs" / "heads" / "config").write_text("0" * 40)

        assert GitState(str(tmp_path)).git_mounts == ["-v", f"{top}:/workspace/.git:ro"]

        git = GitState(str(tmp_path), writable=True)

        ws = "/workspace/.git"
        sub = "modules/group/sub"
        assert git.container_ws == "/workspace"
        assert git.git_mounts == [
            "-v",
            f"{top}:{ws}",
            "-v",
            f"{top}/hooks:{ws}/hooks:ro",
            "-v",
            f"{top}/config:{ws}/config:ro",
            "-v",
            f"{top}/{sub}/hooks:{ws}/{sub}/hooks:ro",
            "-v",
            f"{top}/{sub}/config:{ws}/{sub}/config:ro",
        ]

    def test_worktree_git_file(self, tmp_path):
        # Simulate a worktree: .git is a file, not a directory
        main_repo = tmp_path / "main"
        git_dir = main_repo / ".git"
        make_git_dir(git_dir)

        worktree_dir = tmp_path / "main" / "worktrees" / "feature"
        worktree_dir.mkdir(parents=True)
        (worktree_dir / ".git").write_text(f"gitdir: {git_dir}/worktrees/feature")

        # Create the worktree gitdir, with per-worktree config
        wt_gitdir = git_dir / "worktrees" / "feature"
        wt_gitdir.mkdir(parents=True)
        (wt_gitdir / "HEAD").write_text("ref: refs/heads/feature\n")
        (wt_gitdir / "config.worktree").write_text("[core]\n")
        (wt_gitdir / "commondir").write_text("../..\n")
        (wt_gitdir / "gitdir").write_text(f"{worktree_dir}/.git\n")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = str(git_dir) + "\n"

            readonly = GitState(str(worktree_dir))
            git = GitState(str(worktree_dir), writable=True)

        # Host paths are kept, so no symlink is needed in the container.
        pointer = f"{worktree_dir}/.git"
        assert readonly.container_ws == str(worktree_dir)
        assert readonly.git_mounts == [
            "-v",
            f"{git_dir}:{git_dir}:ro",
            "-v",
            f"{pointer}:{pointer}:ro",
        ]

        wt_config = f"{wt_gitdir}/config.worktree"
        assert git.container_ws == str(worktree_dir)
        assert git.git_mounts == [
            "-v",
            f"{git_dir}:{git_dir}",
            "-v",
            f"{git_dir}/hooks:{git_dir}/hooks:ro",
            "-v",
            f"{git_dir}/config:{git_dir}/config:ro",
            "-v",
            f"{wt_config}:{wt_config}:ro",
            "-v",
            f"{wt_gitdir}/commondir:{wt_gitdir}/commondir:ro",
            "-v",
            f"{wt_gitdir}/gitdir:{wt_gitdir}/gitdir:ro",
            "-v",
            f"{pointer}:{pointer}:ro",
        ]
