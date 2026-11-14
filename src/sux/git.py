import shlex
import subprocess
import sys
from functools import cached_property
from pathlib import Path

# How a submodule URL that points at this machine starts.
LOCAL_URL = ("/", "./", "../", "file:")

# Entries of a git dir a sandbox must not change even when it may write
# git state. `commondir` and `gitdir` exist only in worktree git dirs.
GIT_PROTECTED = ("hooks", "config", "config.worktree", "commondir", "gitdir")


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


class GitRepository:
    """One checkout, resolved once by git.

    sux never writes a file under a git dir: worktrees go in
    `<main checkout>/worktrees/<name>`, ignored by a `.gitignore` inside
    that directory, and anything that would land under a git dir is refused.
    """

    def __init__(self, checkout, git_dir, common_dir):
        self.checkout = Path(checkout).resolve()
        self.git_dir = Path(git_dir).resolve()
        self.common_dir = Path(common_dir).resolve()

    @classmethod
    def find(cls, path):
        """The checkout containing `path`, or None outside a work tree."""
        listing = _git_out(
            path,
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--absolute-git-dir",
            "--git-common-dir",
        )
        if listing is None:
            return None
        return cls(*listing.splitlines())

    @classmethod
    def at(cls, path):
        """The checkout whose root is `path`, or None."""
        path = Path(path)
        return cls.find(path) if (path / ".git").exists() else None

    @classmethod
    def here(cls):
        """The checkout containing the current directory."""
        repo = cls.find(Path.cwd())
        if repo is None:
            _fail(f"{Path.cwd()} is not inside a git repository")
        return repo

    @cached_property
    def main_checkout(self):
        """The main worktree's checkout, never a git dir.

        `git worktree list` derives this from the common dir and ignores
        `core.worktree`, so for a submodule it names the git dir under the
        superproject's `.git/modules`. Asking git from the common dir honours
        `core.worktree`; an ordinary `.git` sits in its checkout.
        """
        top = _git_out(self.common_dir, "rev-parse", "--show-toplevel")
        if top:
            return Path(top).resolve()
        if self.common_dir.name == ".git":
            return self.common_dir.parent
        return _fail(f"{self.common_dir} has no main checkout (bare repository?)")

    def _outside_git(self, path):
        """`path`, or exit if it lies inside a git dir."""
        resolved = path.resolve()
        if ".git" in resolved.parts or resolved.is_relative_to(self.common_dir):
            _fail(
                f"worktree {path} is inside a git dir; remove it with "
                f"`git worktree remove --force {path}` (or delete it and run "
                "`git worktree prune`) and run sux again"
            )
        return path

    def _worktrees(self):
        """List (path, branch ref or None) for every worktree, main first."""
        listing = _git_out(self.checkout, "worktree", "list", "--porcelain") or ""
        entries = []
        for block in listing.split("\n\n"):
            fields = dict(line.partition(" ")[::2] for line in block.splitlines())
            if "worktree" in fields:
                entries.append((Path(fields["worktree"]), fields.get("branch")))
        # git misreports the main worktree of a submodule as its git dir
        entries[0] = (self.main_checkout, entries[0][1])
        return entries

    def worktree(self, name):
        """Create or reuse the worktree for branch `name`, submodules ready."""
        main = self.main_checkout
        target = main / "worktrees" / name
        ref = f"refs/heads/{name}"

        # The branch may already be checked out somewhere else.
        entries = self._worktrees()
        found = [path for path, branch in entries if branch == ref]
        found += [path for path, _ in entries if path == target]
        if found and found[0].is_dir():
            path = self._outside_git(found[0])
            print(f"Using worktree: {path}")
        else:
            path = self._outside_git(target)
            ignore = target.parent / ".gitignore"
            if not ignore.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                ignore.write_text("*\n")
            exists = _git(main, "show-ref", "--verify", "--quiet", ref).returncode == 0
            args = [str(target), name] if exists else ["-b", name, str(target)]
            if not _git_show(main, "worktree", "add", *args):
                _fail(f"could not create worktree {target}")
            print(f"Created worktree: {path}")

        repo = GitRepository.at(path)
        if repo is None:
            _fail(f"{path} is not a git checkout")
        repo.init_submodules(main)
        return repo

    def _submodules(self):
        """Yield (name, path, url) for each submodule checked in here."""
        listing = _git_out(
            self.checkout,
            "config",
            "-f",
            ".gitmodules",
            "--get-regexp",
            r"\.(path|url)$",
        )
        fields = {}
        for line in (listing or "").splitlines():
            key, _, value = line.partition(" ")
            name, _, field = key.removeprefix("submodule.").rpartition(".")
            fields.setdefault(name, {})[field] = value

        base = _git_out(self.checkout, "remote", "get-url", "origin")
        for name, field in fields.items():
            if "path" not in field or "url" not in field:
                continue
            staged = _git_out(self.checkout, "ls-files", "--stage", "--", field["path"])
            if (staged or "").startswith("160000"):
                url = _resolve_url(field["url"], base or str(self.checkout))
                yield name, field["path"], url

    def _update_submodule(self, path, config, *flags):
        """`git submodule update --init` for one path under per-command config."""
        overrides = [arg for item in config for arg in ("-c", item)]
        return _git_show(
            self.checkout,
            *overrides,
            "submodule",
            "update",
            "--init",
            *flags,
            "--",
            path,
        )

    def _init_submodule(self, name, path, url, local):
        """Clone and check out one submodule; `local` is the main checkout's copy.

        The URL is forced per command, so a URL in the repo config is never
        used and nothing is written there. Returns True on success.
        """
        key = f"submodule.{name}.url"
        reference = ["--reference", str(local), "--dissociate"] if local else []
        if self._update_submodule(path, [f"{key}={url}"], *reference):
            return True
        if local is None:
            return False

        sub = self.checkout / path
        if (sub / ".git").exists():
            # Cloned, but the pinned commit was never pushed: take that one
            # commit from the main checkout and check it out without fetching.
            pin = _git_out(self.checkout, "rev-parse", f":{path}")
            print(
                f"sux: fetching unpushed {path} commit {pin} from {local}",
                file=sys.stderr,
            )
            fetched = _git(sub, "fetch", str(local), pin).returncode == 0
            return fetched and self._update_submodule(
                path, [f"{key}={url}"], "--no-fetch"
            )

        print(
            f"sux: could not clone {url}; cloning {path} from {local} instead",
            file=sys.stderr,
        )
        cloned = self._update_submodule(
            path, [f"{key}={local}", "protocol.file.allow=always"]
        )
        if (sub / ".git").exists():
            _git(sub, "remote", "set-url", "origin", url)
        return cloned

    def init_submodules(self, main):
        """Initialise submodules level by level, with URLs from `.gitmodules`.

        `main` is the checkout at the same level in the main worktree; its
        submodule clones are used as a local object source.
        """
        for name, path, url in self._submodules():
            key = f"submodule.{name}.url"
            configured = _git_out(self.checkout, "config", "--get", key)
            if configured and configured != url and configured.startswith(LOCAL_URL):
                sync = shlex.join(
                    ["git", "-C", str(self.checkout), "submodule", "sync", path]
                )
                print(
                    f"sux: warning: submodule {path} has the local URL {configured} "
                    f"in git config (ignored); repair it with: {sync}",
                    file=sys.stderr,
                )

            local = GitRepository.at(Path(main) / path)
            if not self._init_submodule(
                name, path, url, local.git_dir if local else None
            ):
                _fail(f"could not initialise submodule {path} in {self.checkout}")
            if not configured:
                # Forcing the URL skips what `submodule init` would have recorded.
                _git(self.checkout, "config", key, url)

            GitRepository.at(self.checkout / path).init_submodules(Path(main) / path)

    @property
    def _mounted_git_dir(self):
        """The outermost git dir to mount: the `.git` holding the common dir."""
        if self.common_dir.name == ".git":
            return self.common_dir
        return next(
            (p for p in self.common_dir.parents if p.name == ".git"), self.common_dir
        )

    def docker_mounts(self, writable):
        """(workspace path in the container, `-v` args for git state).

        A checkout whose `.git` is a directory is mounted at `/workspace`.
        A worktree or submodule keeps its host paths, so relative and
        absolute `gitdir:` pointers both resolve, and its own pointer is
        read-only so it cannot be aimed at another git dir.
        """
        pointer = self.checkout / ".git"
        if pointer.is_dir():
            return "/workspace", _git_mounts(pointer, "/workspace/.git", writable)
        top = self._mounted_git_dir
        return str(self.checkout), [
            *_git_mounts(top, str(top), writable),
            "-v",
            f"{pointer}:{pointer}:ro",
        ]
