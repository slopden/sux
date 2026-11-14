"""`GitRepository` worktrees and submodules against real temporary repos."""

import os
from pathlib import Path

import pytest

from sux.git import GitRepository, _resolve_url
from tests.conftest import GLOBAL_CONFIG, commit_file, git, out


def open_worktree(name):
    """What `sux -w <name>` does: create or reuse, then step into it."""
    worktree = GitRepository.here().worktree(name).checkout
    os.chdir(worktree)
    return worktree


def mounts(path, writable=False):
    """(container workspace, git `-v` args) for a checkout root."""
    return GitRepository.at(path).docker_mounts(writable)


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

        open_worktree("feature")

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

        # the worktrees directory ignores itself; nothing is written in .git
        assert (repos.main / "worktrees" / ".gitignore").read_text() == "*\n"
        assert out(repos.main, "status", "--porcelain") == ""
        assert not (repos.main / ".git" / "info" / "exclude").exists() or (
            "worktrees" not in (repos.main / ".git" / "info" / "exclude").read_text()
        )

    def test_rerun_reuses_and_resumes(self, repos, monkeypatch, capfd):
        monkeypatch.chdir(repos.main)
        open_worktree("feature")
        worktree = repos.main / "worktrees" / "feature"

        # from inside the worktree: no nested worktrees/ directory
        open_worktree("feature")

        assert Path.cwd() == worktree
        assert not (worktree / "worktrees").exists()
        assert f"Using worktree: {worktree}" in capfd.readouterr().out
        assert (repos.main / "worktrees" / ".gitignore").read_text() == "*\n"
        assert_submodules_ready(repos, worktree)

    def test_branch_checked_out_elsewhere_is_reused(self, repos, monkeypatch):
        elsewhere = repos.tmp / "elsewhere"
        git(repos.main, "worktree", "add", str(elsewhere), "-b", "other")
        monkeypatch.chdir(repos.main)

        open_worktree("other")

        assert Path.cwd() == elsewhere
        assert not (repos.main / "worktrees").exists()
        assert_submodules_ready(repos, elsewhere)

    def test_existing_branch_is_checked_out(self, repos, monkeypatch):
        first = out(repos.main, "rev-list", "--max-parents=0", "HEAD")
        git(repos.main, "branch", "old", first)
        monkeypatch.chdir(repos.main)

        open_worktree("old")

        worktree = repos.main / "worktrees" / "old"
        assert out(worktree, "rev-parse", "HEAD") == first
        assert out(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "old"

    def test_tag_with_that_name_still_gets_a_new_branch(self, repos, monkeypatch):
        first = out(repos.main, "rev-list", "--max-parents=0", "HEAD")
        git(repos.main, "tag", "v1", first)
        monkeypatch.chdir(repos.main)

        open_worktree("v1")

        worktree = repos.main / "worktrees" / "v1"
        assert out(worktree, "symbolic-ref", "HEAD") == "refs/heads/v1"
        assert out(worktree, "rev-parse", "HEAD") == out(
            repos.main, "rev-parse", "HEAD"
        )

    def test_outside_a_repo_exits_with_a_reason(self, tmp_path, git_env, monkeypatch):  # noqa: ARG002
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exit_info:
            open_worktree("feature")
        assert "not inside a git repository" in str(exit_info.value.code)

    def test_failed_worktree_add_exits_with_a_reason(self, repos, monkeypatch):
        # a file where the worktree should go
        (repos.main / "worktrees").mkdir()
        (repos.main / "worktrees" / "feature").write_text("in the way")
        monkeypatch.chdir(repos.main)
        with pytest.raises(SystemExit) as exit_info:
            open_worktree("feature")
        assert "could not create worktree" in str(exit_info.value.code)


class TestInitSubmodules:
    def test_clone_failure_falls_back_to_local_copy(
        self, repos, git_env, monkeypatch, capfd
    ):
        """The remotes become unreachable: the file protocol is blocked."""
        git_env.write_text(GLOBAL_CONFIG.split("[protocol")[0])
        before = shared_config(repos)
        monkeypatch.chdir(repos.main)

        open_worktree("feature")

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
            GitRepository.at(worktree).init_submodules(repos.tmp / "no-main-checkout")
        assert "could not initialise submodule sub" in str(exit_info.value.code)

    def test_missing_url_is_recorded_like_submodule_init(self, repos, monkeypatch):
        git(repos.main, "config", "--unset", "submodule.sub.url")
        git(repos.main, "config", "--unset", "submodule.active")
        monkeypatch.chdir(repos.main)

        open_worktree("feature")

        worktree = repos.main / "worktrees" / "feature"
        assert_submodules_ready(repos, worktree)
        assert out(worktree, "config", "submodule.sub.url") == str(repos.urls["sub"])
        nested_url = out(worktree / "sub", "config", "submodule.nested.url")
        assert nested_url == str(repos.urls["nested"])

    def test_suggested_sync_repairs_the_config(self, repos, monkeypatch):
        monkeypatch.chdir(repos.main)
        open_worktree("feature")
        worktree = repos.main / "worktrees" / "feature"

        git(worktree, "submodule", "sync", "sub")

        assert out(repos.main, "config", "submodule.sub.url") == str(repos.urls["sub"])

    def test_gitmodules_entry_without_a_gitlink_is_skipped(self, repos, monkeypatch):
        git(repos.main, "config", "-f", ".gitmodules", "submodule.stale.path", "stale")
        git(repos.main, "config", "-f", ".gitmodules", "submodule.stale.url", "/nope")
        commit_file(repos.main, "other.txt", "stale gitmodules entry")
        monkeypatch.chdir(repos.main)

        open_worktree("feature")

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


class TestDockerMountsRealRepos:
    def test_worktree_with_submodules(self, repos, monkeypatch):
        monkeypatch.chdir(repos.main)
        open_worktree("feature")
        worktree = repos.main / "worktrees" / "feature"
        top = repos.main / ".git"

        ws, readonly = mounts(worktree)
        assert ws == str(worktree)
        assert readonly == [
            "-v",
            f"{top}:{top}:ro",
            "-v",
            f"{worktree}/.git:{worktree}/.git:ro",
        ]

        ws, args = mounts(worktree, writable=True)

        assert ws == str(worktree)
        volumes = args[1::2]
        assert args[0::2] == ["-v"] * len(volumes)
        assert volumes[0] == f"{top}:{top}"
        # everything after the read-write .git is a read-only identity overlay
        overlays = [mount.removesuffix(":ro").split(":") for mount in volumes[1:]]
        assert all(mount.endswith(":ro") for mount in volumes[1:])
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
        assert mounts(sub)[1] == ["-v", f"{top}:{top}:ro", *pointer]

        ws, args = mounts(sub, writable=True)
        assert ws == str(sub)
        assert args[:2] == ["-v", f"{top}:{top}"]
        assert args[-2:] == pointer
        assert f"{top}/modules/sub/hooks:{top}/modules/sub/hooks:ro" in args

    def test_plain_repo_with_submodules(self, repos):
        top = repos.main / ".git"
        assert mounts(repos.main) == ("/workspace", ["-v", f"{top}:/workspace/.git:ro"])

        ws, args = mounts(repos.main, writable=True)
        assert ws == "/workspace"
        assert args[:2] == ["-v", f"{top}:/workspace/.git"]
        assert f"{top}/config:/workspace/.git/config:ro" in args
        assert (
            f"{top}/modules/sub/modules/nested/hooks"
            ":/workspace/.git/modules/sub/modules/nested/hooks:ro"
        ) in args


class TestNeverInsideGit:
    def test_submodule_worktree_goes_in_its_checkout(self, repos, monkeypatch):
        """In a submodule `worktree list` names the git dir as the main worktree."""
        sub = repos.main / "sub"
        common = repos.main / ".git" / "modules" / "sub"
        info = sorted((common / "info").rglob("*"))
        deep = sub / "deep" / "er"
        deep.mkdir(parents=True)
        monkeypatch.chdir(deep)

        worktree = open_worktree("feature")

        assert worktree == sub / "worktrees" / "feature"
        assert ".git" not in worktree.parts
        assert not (common / "worktrees" / "feature" / "sub.txt").exists()
        assert out(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "feature"
        assert (worktree / "nested" / "nested.txt").exists()
        # no git state leaks into the checkout, nothing shows up anywhere
        assert out(worktree, "status", "--porcelain") == ""
        assert out(sub, "status", "--porcelain") == ""
        assert out(repos.main, "status", "--porcelain") == ""
        assert sorted((common / "info").rglob("*")) == info

        # rerun from inside the worktree reuses it
        assert open_worktree("feature") == worktree

    def test_worktree_inside_a_git_dir_is_refused(self, repos, monkeypatch):
        """The layout an earlier sux made: checkout and admin dir in one place."""
        sub = repos.main / "sub"
        bad = repos.main / ".git" / "modules" / "sub" / "worktrees" / "bad"
        git(sub, "worktree", "add", str(bad), "-b", "bad")
        monkeypatch.chdir(sub)

        with pytest.raises(SystemExit) as exit_info:
            open_worktree("bad")

        reason = str(exit_info.value.code)
        assert f"worktree {bad} is inside a git dir" in reason
        assert "git worktree remove --force" in reason
