from unittest.mock import patch

from sux.git import GitState


class TestGitState:
    def test_no_git_dir(self, tmp_path):
        git = GitState(str(tmp_path))
        assert git.git_mounts == []
        assert git.container_ws == "/workspace"
        assert git.container_cmd == ["sleep", "infinity"]

    def test_regular_git_dir(self, tmp_path):
        (tmp_path / ".git").mkdir()
        git = GitState(str(tmp_path))
        assert git.git_mounts == ["-v", f"{tmp_path}/.git:/workspace/.git:ro"]
        assert git.container_ws == "/workspace"

    def test_worktree_git_file(self, tmp_path):
        # Simulate a worktree: .git is a file, not a directory
        main_repo = tmp_path / "main"
        main_repo.mkdir()
        git_dir = main_repo / ".git"
        git_dir.mkdir()

        worktree_dir = tmp_path / "main" / "worktrees" / "feature"
        worktree_dir.mkdir(parents=True)
        (worktree_dir / ".git").write_text(f"gitdir: {git_dir}/worktrees/feature")

        # Create the worktree gitdir
        wt_gitdir = git_dir / "worktrees" / "feature"
        wt_gitdir.mkdir(parents=True)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = str(git_dir) + "\n"

            git = GitState(str(worktree_dir))

        assert git.git_mounts == ["-v", f"{git_dir}:/.git:ro"]
        assert git.container_cmd[0] == "bash"
