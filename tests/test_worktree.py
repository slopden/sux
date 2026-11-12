"""`ensure_worktree` and `init_submodules` against real temporary repos."""

from pathlib import Path

import pytest

from sux.git import GitState, _resolve_url, ensure_worktree, init_submodules
from tests.conftest import GLOBAL_CONFIG, commit_file, git, out


def shared_config(repos):
    return (repos.main / ".git" / "config").read_text()


def assert_submodules_ready(repos, worktree):
    sub = worktree / "sub"
    assert out(sub, "rev-parse", "HEAD") == repos.pin
    assert (sub / "unpushed.txt").exists()
    assert (sub / "nested" / "nested.txt").exists()
    assert out(sub, "remote", "get-url", "origin") == str(repos.urls["sub"])
    nested_origin = out(sub / "nested", "remote", "get-url", "origin")
    assert nested_origin == str(repos.urls["nested"])
    # nothing uninitialised or out of date at any level
    status = git(worktree, "submodule", "status", "--recursive").stdout.splitlines()
    assert [line[0] for line in status] == [" ", " "]


class TestEnsureWorktree:
    def test_created_at_repo_root_from_subdirectory(self, repos, monkeypatch, capfd):
        subdir = repos.main / "deep" / "er"
        subdir.mkdir(parents=True)
        monkeypatch.chdir(subdir)
        before = shared_config(repos)

        ensure_worktree("feature")

        worktree = repos.main / "worktrees" / "feature"
        assert Path.cwd() == worktree
        assert out(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "feature"
        assert not (subdir / "worktrees").exists()
        assert_submodules_ready(repos, worktree)

        # the bad URL is neither used, changed nor joined by another path
        assert shared_config(repos) == before
        assert str(repos.main) not in out(
            worktree / "sub", "config", "--local", "--get-regexp", "url"
        )
        err = capfd.readouterr().err
        assert str(repos.poison) in err
        assert "submodule sync sub" in err

        # the worktrees directory does not show up as untracked
        assert "worktrees" not in out(repos.main, "status", "--porcelain")

    def test_rerun_reuses_and_resumes(self, repos, monkeypatch, capfd):
        monkeypatch.chdir(repos.main)
        ensure_worktree("feature")
        worktree = repos.main / "worktrees" / "feature"
        exclude = (repos.main / ".git" / "info" / "exclude").read_text()

        # from inside the worktree: no nested worktrees/ directory
        ensure_worktree("feature")

        assert Path.cwd() == worktree
        assert not (worktree / "worktrees").exists()
        assert f"Using worktree: {worktree}" in capfd.readouterr().out
        assert (repos.main / ".git" / "info" / "exclude").read_text() == exclude
        assert exclude.count("/worktrees/") == 1
        assert_submodules_ready(repos, worktree)

    def test_branch_checked_out_elsewhere_is_reused(self, repos, monkeypatch):
        elsewhere = repos.tmp / "elsewhere"
        git(repos.main, "worktree", "add", str(elsewhere), "-b", "other")
        monkeypatch.chdir(repos.main)

        ensure_worktree("other")

        assert Path.cwd() == elsewhere
        assert not (repos.main / "worktrees").exists()
        assert_submodules_ready(repos, elsewhere)

    def test_existing_branch_is_checked_out(self, repos, monkeypatch):
        first = out(repos.main, "rev-list", "--max-parents=0", "HEAD")
        git(repos.main, "branch", "old", first)
        monkeypatch.chdir(repos.main)

        ensure_worktree("old")

        worktree = repos.main / "worktrees" / "old"
        assert out(worktree, "rev-parse", "HEAD") == first
        assert out(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "old"

    def test_tag_with_that_name_still_gets_a_new_branch(self, repos, monkeypatch):
        first = out(repos.main, "rev-list", "--max-parents=0", "HEAD")
        git(repos.main, "tag", "v1", first)
        monkeypatch.chdir(repos.main)

        ensure_worktree("v1")

        worktree = repos.main / "worktrees" / "v1"
        assert out(worktree, "symbolic-ref", "HEAD") == "refs/heads/v1"
        assert out(worktree, "rev-parse", "HEAD") == out(
            repos.main, "rev-parse", "HEAD"
        )

    def test_outside_a_repo_exits_with_a_reason(self, tmp_path, git_env, monkeypatch):  # noqa: ARG002
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exit_info:
            ensure_worktree("feature")
        assert "not inside a git repository" in str(exit_info.value.code)

    def test_failed_worktree_add_exits_with_a_reason(self, repos, monkeypatch):
        # a file where the worktree should go
        (repos.main / "worktrees").mkdir()
        (repos.main / "worktrees" / "feature").write_text("in the way")
        monkeypatch.chdir(repos.main)
        with pytest.raises(SystemExit) as exit_info:
            ensure_worktree("feature")
        assert "could not create worktree" in str(exit_info.value.code)


class TestInitSubmodules:
    def test_clone_failure_falls_back_to_local_copy(
        self, repos, git_env, monkeypatch, capfd
    ):
        """The remotes become unreachable: the file protocol is blocked."""
        git_env.write_text(GLOBAL_CONFIG.split("[protocol")[0])
        before = shared_config(repos)
        monkeypatch.chdir(repos.main)

        ensure_worktree("feature")

        assert_submodules_ready(repos, repos.main / "worktrees" / "feature")
        assert shared_config(repos) == before
        assert "protocol" not in git_env.read_text()
        err = capfd.readouterr().err
        assert f"could not clone {repos.urls['sub']}" in err
        assert f"could not clone {repos.urls['nested']}" in err

    def test_no_local_copy_and_unpushed_pin_exits_with_a_reason(self, repos):
        worktree = repos.main / "worktrees" / "wt"
        git(repos.main, "worktree", "add", str(worktree), "-b", "wt")
        with pytest.raises(SystemExit) as exit_info:
            init_submodules(worktree, repos.tmp / "no-main-checkout")
        assert "could not initialise submodule sub" in str(exit_info.value.code)

    def test_missing_url_is_recorded_like_submodule_init(self, repos, monkeypatch):
        git(repos.main, "config", "--unset", "submodule.sub.url")
        git(repos.main, "config", "--unset", "submodule.active")
        monkeypatch.chdir(repos.main)

        ensure_worktree("feature")

        worktree = repos.main / "worktrees" / "feature"
        assert_submodules_ready(repos, worktree)
        assert out(worktree, "config", "submodule.sub.url") == str(repos.urls["sub"])
        nested_url = out(worktree / "sub", "config", "submodule.nested.url")
        assert nested_url == str(repos.urls["nested"])

    def test_suggested_sync_repairs_the_config(self, repos, monkeypatch):
        monkeypatch.chdir(repos.main)
        ensure_worktree("feature")
        worktree = repos.main / "worktrees" / "feature"

        git(worktree, "submodule", "sync", "sub")

        assert out(repos.main, "config", "submodule.sub.url") == str(repos.urls["sub"])

    def test_gitmodules_entry_without_a_gitlink_is_skipped(self, repos, monkeypatch):
        git(repos.main, "config", "-f", ".gitmodules", "submodule.stale.path", "stale")
        git(repos.main, "config", "-f", ".gitmodules", "submodule.stale.url", "/nope")
        commit_file(repos.main, "other.txt", "stale gitmodules entry")
        monkeypatch.chdir(repos.main)

        ensure_worktree("feature")

        assert_submodules_ready(repos, repos.main / "worktrees" / "feature")


class TestResolveUrl:
    def test_absolute_is_unchanged(self):
        url = "https://example.com/org/sub.git"
        assert _resolve_url(url, "https://example.com/org/super.git") == url

    def test_sibling(self):
        resolved = _resolve_url("../sub.git", "https://example.com/org/super.git")
        assert resolved == "https://example.com/org/sub.git"

    def test_two_levels_up_and_trailing_slash(self):
        resolved = _resolve_url("../../other/sub", "https://example.com/org/super/")
        assert resolved == "https://example.com/other/sub"

    def test_child(self):
        resolved = _resolve_url("./sub.git", "https://example.com/org/super")
        assert resolved == "https://example.com/org/super/sub.git"

    def test_scp_style(self):
        resolved = _resolve_url("../sub.git", "git@example.com:org/super.git")
        assert resolved == "git@example.com:org/sub.git"
        assert _resolve_url("../sub.git", "git@example.com:super.git") == (
            "git@example.com:sub.git"
        )


class TestGitStateRealRepos:
    def test_worktree_with_submodules(self, repos, monkeypatch):
        monkeypatch.chdir(repos.main)
        ensure_worktree("feature")
        worktree = repos.main / "worktrees" / "feature"
        top = repos.main / ".git"

        readonly = GitState(str(worktree))
        assert readonly.container_ws == str(worktree)
        assert readonly.git_mounts == [
            "-v",
            f"{top}:{top}:ro",
            "-v",
            f"{worktree}/.git:{worktree}/.git:ro",
        ]

        state = GitState(str(worktree), writable=True)

        assert state.container_ws == str(worktree)
        mounts = state.git_mounts[1::2]
        assert state.git_mounts[0::2] == ["-v"] * len(mounts)
        assert mounts[0] == f"{top}:{top}"
        # everything after the read-write .git is a read-only identity overlay
        overlays = [mount.removesuffix(":ro").split(":") for mount in mounts[1:]]
        assert all(mount.endswith(":ro") for mount in mounts[1:])
        assert all(src == dst for src, dst in overlays)
        assert overlays.pop() == [f"{worktree}/.git"] * 2
        protected = {Path(src).relative_to(top).as_posix() for src, _ in overlays}
        assert protected == {
            "worktrees/feature/commondir",
            "worktrees/feature/gitdir",
            "config",
            "hooks",
            "modules/sub/config",
            "modules/sub/hooks",
            "modules/sub/modules/nested/config",
            "modules/sub/modules/nested/hooks",
            "worktrees/feature/modules/sub/config",
            "worktrees/feature/modules/sub/hooks",
            "worktrees/feature/modules/sub/modules/nested/config",
            "worktrees/feature/modules/sub/modules/nested/hooks",
        }

    def test_submodule_directory(self, repos):
        sub = repos.main / "sub"
        top = repos.main / ".git"
        pointer = ["-v", f"{sub}/.git:{sub}/.git:ro"]
        assert GitState(str(sub)).git_mounts == ["-v", f"{top}:{top}:ro", *pointer]

        state = GitState(str(sub), writable=True)
        assert state.container_ws == str(sub)
        assert state.git_mounts[:2] == ["-v", f"{top}:{top}"]
        assert state.git_mounts[-2:] == pointer
        assert f"{top}/modules/sub/hooks:{top}/modules/sub/hooks:ro" in state.git_mounts

    def test_plain_repo_with_submodules(self, repos):
        top = repos.main / ".git"
        assert GitState(str(repos.main)).git_mounts == [
            "-v",
            f"{top}:/workspace/.git:ro",
        ]

        state = GitState(str(repos.main), writable=True)
        assert state.container_ws == "/workspace"
        assert state.git_mounts[:2] == ["-v", f"{top}:/workspace/.git"]
        assert f"{top}/config:/workspace/.git/config:ro" in state.git_mounts
        assert (
            f"{top}/modules/sub/modules/nested/hooks"
            ":/workspace/.git/modules/sub/modules/nested/hooks:ro"
        ) in state.git_mounts
