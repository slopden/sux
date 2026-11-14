"""`GitRepository.docker_mounts` on real repos with hand-made extra git dirs."""

from sux.git import GitRepository
from tests.conftest import commit_file, git


def make_git_dir(path):
    """A directory that looks like a git dir with hooks and config."""
    (path / "hooks").mkdir(parents=True)
    (path / "HEAD").write_text("ref: refs/heads/main\n")
    (path / "config").write_text("[core]\n")


def new_repo(path):
    git(path.parent, "init", str(path))
    commit_file(path, "file.txt", "first")
    return path


class TestDockerMounts:
    def test_no_git_dir(self, tmp_path):
        assert GitRepository.at(tmp_path) is None

    def test_regular_git_dir(self, tmp_path, git_env):  # noqa: ARG002
        repo = new_repo(tmp_path / "repo")
        top = repo / ".git"
        make_git_dir(top / "modules" / "group" / "sub")
        # a branch called `config` and a ref namespace called `hooks`
        git(repo, "branch", "config")
        git(repo, "branch", "hooks/x")

        assert GitRepository.at(repo).docker_mounts(False) == (
            "/workspace",
            ["-v", f"{top}:/workspace/.git:ro"],
        )

        ws, mounts = GitRepository.at(repo).docker_mounts(True)

        dst = "/workspace/.git"
        sub = "modules/group/sub"
        assert ws == "/workspace"
        assert mounts == [
            "-v",
            f"{top}:{dst}",
            "-v",
            f"{top}/hooks:{dst}/hooks:ro",
            "-v",
            f"{top}/config:{dst}/config:ro",
            "-v",
            f"{top}/{sub}/hooks:{dst}/{sub}/hooks:ro",
            "-v",
            f"{top}/{sub}/config:{dst}/{sub}/config:ro",
        ]

    def test_worktree_git_file(self, tmp_path, git_env):  # noqa: ARG002
        repo = new_repo(tmp_path / "main")
        git_dir = repo / ".git"
        git(repo, "config", "extensions.worktreeConfig", "true")
        worktree = repo / "worktrees" / "feature"
        git(repo, "worktree", "add", str(worktree), "-b", "feature")
        git(worktree, "config", "--worktree", "core.bare", "false")
        wt_gitdir = git_dir / "worktrees" / "feature"

        ws, readonly = GitRepository.at(worktree).docker_mounts(False)

        # Host paths are kept, so no symlink is needed in the container.
        pointer = f"{worktree}/.git"
        assert ws == str(worktree)
        assert readonly == [
            "-v",
            f"{git_dir}:{git_dir}:ro",
            "-v",
            f"{pointer}:{pointer}:ro",
        ]

        ws, mounts = GitRepository.at(worktree).docker_mounts(True)

        wt_config = f"{wt_gitdir}/config.worktree"
        assert ws == str(worktree)
        assert mounts == [
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
