"""Git behaviours `sux.git.init_submodules` relies on, against real repos.

Each test runs plain git, no sux code, so a git upgrade that changes one of
these shows up here first.
"""

from tests.conftest import GLOBAL_CONFIG, git, out

NO_FILE_PROTOCOL = GLOBAL_CONFIG.split("[protocol")[0]


def add_worktree(repos):
    worktree = repos.main / "worktrees" / "wt"
    git(repos.main, "worktree", "add", str(worktree), "-b", "wt")
    return worktree


def push_pin(repos):
    git(repos.main / "sub", "push", "origin", "HEAD:main")


def update(repo, name, url, *flags, check=False):
    """`submodule update --init` with the URL forced for this command only."""
    override = f"submodule.{name}.url={url}"
    return git(
        repo, "-c", override, "submodule", "update", "--init", *flags, "--", name,
        check=check,
    )  # fmt: skip


def reference(checkout):
    return [
        "--reference",
        out(checkout, "rev-parse", "--absolute-git-dir"),
        "--dissociate",
    ]


def shared_config(repos):
    return (repos.main / ".git" / "config").read_text()


def test_config_url_wins_over_gitmodules(repos):
    """Without an override the local path in shared config is what is cloned."""
    push_pin(repos)
    worktree = add_worktree(repos)
    result = git(worktree, "submodule", "update", "--init", "--", "sub", check=False)
    assert result.returncode != 0
    assert str(repos.poison) in result.stderr


def test_override_url_is_used_and_not_written(repos):
    push_pin(repos)
    worktree = add_worktree(repos)
    before = shared_config(repos)

    update(
        worktree, "sub", repos.urls["sub"], *reference(repos.main / "sub"), check=True
    )

    sub = worktree / "sub"
    # (a) cloned from the override, (b) which is what origin is left as
    assert out(sub, "remote", "get-url", "origin") == str(repos.urls["sub"])
    assert out(sub, "rev-parse", "HEAD") == repos.pin
    # (c) shared config untouched: the poison stays, nothing else is added
    assert shared_config(repos) == before
    # --dissociate leaves no link to the main checkout's objects
    git_dir = out(sub, "rev-parse", "--absolute-git-dir")
    assert not (repos.tmp / git_dir / "objects" / "info" / "alternates").exists()
    assert str(repos.main) not in out(sub, "config", "--local", "--list").replace(
        "core.worktree", ""
    )


def test_override_alone_leaves_submodule_uninitialised(repos):
    """With no URL in config the override records nothing at all.

    The checkout is complete but git still reports the submodule as not
    initialised, so sux has to record the URL itself.
    """
    worktree = add_worktree(repos)
    update(worktree, "sub", repos.urls["sub"])
    git(
        worktree / "sub",
        "fetch",
        out(repos.main / "sub", "rev-parse", "--git-dir"),
        repos.pin,
    )
    update(worktree, "sub", repos.urls["sub"], "--no-fetch", check=True)
    sub = worktree / "sub"

    update(sub, "nested", repos.urls["nested"], check=True)

    assert (sub / "nested" / "nested.txt").exists()
    assert (
        git(sub, "config", "--get", "submodule.nested.url", check=False).returncode != 0
    )
    assert out(sub, "submodule", "status").startswith("-")
    git(sub, "config", "submodule.nested.url", str(repos.urls["nested"]))
    assert not out(sub, "submodule", "status").startswith("-")


def test_unpushed_pin_fails_then_local_fetch_and_retry_works(repos):
    worktree = add_worktree(repos)
    before = shared_config(repos)
    flags = reference(repos.main / "sub")

    # (d) the clone succeeds, the checkout does not
    failed = update(worktree, "sub", repos.urls["sub"], *flags)
    assert failed.returncode != 0
    assert f"did not contain {repos.pin}" in failed.stderr
    sub = worktree / "sub"
    assert (sub / ".git").exists()
    assert not (sub / "sub.txt").exists()
    assert git(sub, "cat-file", "-e", repos.pin, check=False).returncode != 0

    # fetch that one commit from the main checkout's submodule git dir
    main_git_dir = out(repos.main / "sub", "rev-parse", "--absolute-git-dir")
    git(sub, "fetch", main_git_dir, repos.pin)

    # retry; --no-fetch checks out what is now local without the remote
    update(worktree, "sub", repos.urls["sub"], "--no-fetch", check=True)
    assert out(sub, "rev-parse", "HEAD") == repos.pin
    assert (sub / "unpushed.txt").exists()
    assert out(sub, "remote", "get-url", "origin") == str(repos.urls["sub"])
    assert shared_config(repos) == before


def test_file_protocol_can_be_allowed_for_one_command(repos, git_env):
    """A blocked clone can be redone from a local copy; origin is then reset."""
    worktree = add_worktree(repos)
    git_env.write_text(NO_FILE_PROTOCOL)
    before = shared_config(repos)
    main_git_dir = out(repos.main / "sub", "rev-parse", "--absolute-git-dir")

    blocked = update(worktree, "sub", repos.urls["sub"])
    assert blocked.returncode != 0
    assert "transport 'file' not allowed" in blocked.stderr
    assert not (worktree / "sub" / ".git").exists()

    git(
        worktree,
        "-c",
        "protocol.file.allow=always",
        "-c",
        f"submodule.sub.url={main_git_dir}",
        "submodule",
        "update",
        "--init",
        "--",
        "sub",
    )
    sub = worktree / "sub"
    assert out(sub, "rev-parse", "HEAD") == repos.pin
    assert out(sub, "remote", "get-url", "origin") == main_git_dir
    git(sub, "remote", "set-url", "origin", str(repos.urls["sub"]))

    assert shared_config(repos) == before
    assert git_env.read_text() == NO_FILE_PROTOCOL
    assert "protocol.file" not in out(sub, "config", "--local", "--list")
