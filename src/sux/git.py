import os
import shlex
import subprocess
import sys
from pathlib import Path

# Anchored so only the directory at the repo root is ignored.
WORKTREE_EXCLUDE = "/worktrees/"

# How a submodule URL that points at this machine starts.
LOCAL_URL = ("/", "./", "../", "file:")

# Entries of a git dir a sandbox must not change even when it may write
# git state. `commondir` and `gitdir` exist only in worktree git dirs.
GIT_PROTECTED = ("hooks", "config", "config.worktree", "commondir", "gitdir")


def _git_dirs(path):
    """Yield every git dir at or below `path`: itself, modules, worktrees."""
    if (path / "HEAD").is_file():
        yield path
        children = [path / "modules", path / "worktrees"]
    else:
        children = sorted(path.iterdir())
    for child in children:
        if child.is_dir() and not child.is_symlink():
            yield from _git_dirs(child)


def _git_mounts(top_git, container_git, writable):
    """Mount `.git` read-only, or writable with its hooks and config read-only.

    The writable form is best-effort: a git dir under `modules` or
    `worktrees` can still be renamed and replaced from inside the sandbox.
    """
    if not writable:
        return ["-v", f"{top_git}:{container_git}:ro"]
    mounts = ["-v", f"{top_git}:{container_git}"]
    for git_dir in _git_dirs(top_git):
        for name in GIT_PROTECTED:
            if (git_dir / name).exists():
                rel = (git_dir / name).relative_to(top_git)
                mounts += ["-v", f"{top_git / rel}:{container_git}/{rel}:ro"]
    return mounts


class GitState:
    """Resolve git directory layout for Docker mounts."""

    def __init__(self, host_dir, writable=False):
        self.git_mounts = []
        self.container_ws = "/workspace"
        git_dir = Path(host_dir) / ".git"

        if not git_dir.exists():
            return

        if git_dir.is_dir():
            self.git_mounts = _git_mounts(git_dir, "/workspace/.git", writable)
            return

        # Worktree or submodule: .git is a file pointing to a gitdir path.
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

        # Same paths inside the container as on the host, so relative and
        # absolute `gitdir:` pointers both resolve. The workspace's own
        # pointer is read-only so it cannot be aimed at another git dir.
        self.container_ws = host_dir
        self.git_mounts = [
            *_git_mounts(top_git, str(top_git), writable),
            "-v",
            f"{git_dir}:{git_dir}:ro",
        ]


def _fail(reason):
    """Exit non-zero with a one-line reason and no traceback."""
    sys.exit(f"sux: {reason}")


def _git(cwd, *args):
    """Run git in `cwd` with its output captured."""
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _git_out(cwd, *args):
    """Stdout of a git command, or None if it failed."""
    result = _git(cwd, *args)
    return result.stdout.strip() if result.returncode == 0 else None


def _git_show(cwd, *args):
    """Run git in `cwd` with its output shown. Returns True on success."""
    return subprocess.run(["git", "-C", str(cwd), *args], check=False).returncode == 0


def _worktrees(cwd):
    """List [path, branch ref] for every worktree, main worktree first."""
    listing = _git_out(cwd, "worktree", "list", "--porcelain")
    if not listing:
        _fail(f"{cwd} is not inside a git repository")
    entries = []
    for line in listing.splitlines():
        key, _, value = line.partition(" ")
        if key == "worktree":
            entries.append([Path(value), None])
        elif key == "branch":
            entries[-1][1] = value
    return entries


def _exclude_worktrees(main_root):
    """Keep the worktrees directory out of `git status` in the main checkout."""
    common = _git_out(
        main_root, "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    exclude = Path(common) / "info" / "exclude"
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    if WORKTREE_EXCLUDE not in lines:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("\n".join([*lines, WORKTREE_EXCLUDE]) + "\n")


def ensure_worktree(name):
    """Create or reuse the worktree for branch `name` and chdir into it."""
    entries = _worktrees(Path.cwd())
    main_root = entries[0][0]
    target = main_root / "worktrees" / name
    ref = f"refs/heads/{name}"
    _exclude_worktrees(main_root)

    # The branch may already be checked out somewhere else.
    found = [path for path, branch in entries if branch == ref]
    found += [path for path, _ in entries if path == target]
    if found and found[0].is_dir():
        worktree = found[0]
        print(f"Using worktree: {worktree}")
    else:
        worktree = target
        exists = _git(main_root, "show-ref", "--verify", "--quiet", ref).returncode == 0
        args = [str(target), name] if exists else ["-b", name, str(target)]
        if not _git_show(main_root, "worktree", "add", *args):
            _fail(f"could not create worktree {target}")
        print(f"Created worktree: {worktree}")

    os.chdir(worktree)
    init_submodules(worktree, main_root)


def _resolve_url(url, base):
    """Resolve a relative submodule URL against the superproject's URL."""
    if not url.startswith(("./", "../")):
        return url
    base = base.rstrip("/")
    sep = "/"
    while url.startswith(("./", "../")):
        if url.startswith("./"):
            url = url[2:]
        elif "/" in base:
            url, base = url[3:], base.rpartition("/")[0]
        else:
            # scp-like `host:repo`
            url, base, sep = url[3:], base.rpartition(":")[0], ":"
    return f"{base}{sep}{url}"


def _submodules(repo):
    """Yield (name, path, url) for each submodule checked in to `repo`."""
    listing = _git_out(
        repo, "config", "-f", ".gitmodules", "--get-regexp", r"\.(path|url)$"
    )
    fields = {}
    for line in (listing or "").splitlines():
        key, _, value = line.partition(" ")
        name, _, field = key.removeprefix("submodule.").rpartition(".")
        fields.setdefault(name, {})[field] = value

    base = _git_out(repo, "remote", "get-url", "origin") or str(repo)
    for name, field in fields.items():
        if "path" not in field or "url" not in field:
            continue
        staged = _git_out(repo, "ls-files", "--stage", "--", field["path"]) or ""
        if staged.startswith("160000"):
            yield name, field["path"], _resolve_url(field["url"], base)


def _checkout_git_dir(path):
    """Absolute git dir of the checkout at `path`, or None if there is none."""
    if not (path / ".git").exists():
        return None
    return _git_out(path, "rev-parse", "--absolute-git-dir")


def _update_submodule(repo, path, config, *flags):
    """`git submodule update --init` for one path under per-command config."""
    overrides = [arg for item in config for arg in ("-c", item)]
    return _git_show(
        repo, *overrides, "submodule", "update", "--init", *flags, "--", path
    )


def _init_submodule(repo, name, path, url, local):
    """Clone and check out one submodule; `local` is the main checkout's copy.

    The URL is forced per command, so a URL in the repo config is never used
    and nothing is written there. Returns True on success.
    """
    key = f"submodule.{name}.url"
    reference = ["--reference", local, "--dissociate"] if local else []
    if _update_submodule(repo, path, [f"{key}={url}"], *reference):
        return True
    if local is None:
        return False

    sub = repo / path
    if (sub / ".git").exists():
        # Cloned, but the pinned commit was never pushed: take that one
        # commit from the main checkout and check it out without fetching.
        pin = _git_out(repo, "rev-parse", f":{path}")
        print(
            f"sux: fetching unpushed {path} commit {pin} from {local}",
            file=sys.stderr,
        )
        fetched = _git(sub, "fetch", local, pin).returncode == 0
        return fetched and _update_submodule(repo, path, [f"{key}={url}"], "--no-fetch")

    print(
        f"sux: could not clone {url}; cloning {path} from {local} instead",
        file=sys.stderr,
    )
    cloned = _update_submodule(
        repo, path, [f"{key}={local}", "protocol.file.allow=always"]
    )
    if (sub / ".git").exists():
        _git(sub, "remote", "set-url", "origin", url)
    return cloned


def init_submodules(worktree, main_root):
    """Initialise submodules level by level, with URLs from `.gitmodules`.

    `main_root` is the checkout at the same level in the main worktree; its
    submodule clones are used as a local object source.
    """
    for name, path, url in _submodules(worktree):
        key = f"submodule.{name}.url"
        configured = _git_out(worktree, "config", "--get", key)
        if configured and configured != url and configured.startswith(LOCAL_URL):
            sync = shlex.join(["git", "-C", str(worktree), "submodule", "sync", path])
            print(
                f"sux: warning: submodule {path} has the local URL {configured} "
                f"in git config (ignored); repair it with: {sync}",
                file=sys.stderr,
            )

        local = _checkout_git_dir(main_root / path)
        if not _init_submodule(worktree, name, path, url, local):
            _fail(f"could not initialise submodule {path} in {worktree}")
        if not configured:
            # Forcing the URL skips what `submodule init` would have recorded.
            _git(worktree, "config", key, url)

        init_submodules(worktree / path, main_root / path)
