import difflib
import grp
import hashlib
import ipaddress
import json
import os
import pwd
import re
import secrets
import shlex
import shutil
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from sux.install.log import Fail
from sux.install.model import UNKNOWN, Action, Probe, Resource, register

UNIT_DIR = Path("/etc/systemd/system")
APT_SOURCES = Path("/etc/apt/sources.list.d")
APT_KEYRINGS = Path("/etc/apt/keyrings")
# Either is touched by a successful `apt-get update`.
APT_STAMPS = (
    Path("/var/lib/apt/periodic/update-success-stamp"),
    Path("/var/lib/apt/lists"),
)
APT_MAX_AGE = 24 * 3600
# Blocks service starts while packages install, so a stock config never runs.
POLICY_RC = Path("/usr/sbin/policy-rc.d")
POLICY_RC_TEXT = "#!/bin/sh\nexit 101\n"
MANAGED_BY = "# Managed by "  # start of the header line of a file we write
SECRET_MARK = "\0secret\0"  # where `$secret` lands in a secret step's content
MARKER = ".sux-sha256"  # records which archive an extracted tree came from
HTTP_OK = 200
UNIT_STATES = ("running", "on-demand", "stopped", "masked")
ENABLED = ("enabled", "static")


# ---------------------------------------------------------------- helpers


def parse_mode(value, default):
    """Accept `mode = "0644"` and `mode = 0o644`."""
    if value is None:
        return default
    return value if isinstance(value, int) else int(str(value), 8)


def stat_meta(ctx, path):
    """Return (exists, "owner:group mode"); exists is None when it needs sudo."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return False, "missing"
    except PermissionError:
        r = ctx.sh.probe(["stat", "-c", "%U:%G %a", str(path)], sudo=True)
        if r is None:
            return None, UNKNOWN
        if r.returncode != 0:
            return False, "missing"
        owner, _, mode = r.stdout.strip().partition(" ")
        return True, f"{owner} {int(mode, 8):04o}"
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    return True, f"{owner}:{group} {st.st_mode & 0o7777:04o}"


def describe(content):
    if not content:
        return "empty"
    digest = hashlib.sha256(content.encode()).hexdigest()[:12]
    return f"{len(content.splitlines())} lines, sha256 {digest}"


def body(text):
    """`text` without its "# Managed by ..." comment lines.

    Who wrote a file is not a reason to write it again: the header of an
    older installer counts as ours.
    """
    lines = text.splitlines(keepends=True)
    return "".join(ln for ln in lines if not ln.startswith(MANAGED_BY))


def read_file(ctx, path, secret=False):
    """File content, through sudo if unreadable. None while that needs sudo."""
    path = Path(path)
    if os.access(path, os.R_OK):
        return path.read_text()
    r = ctx.sh.probe(["cat", str(path)], sudo=True, secret_output=secret)
    if r is None:
        return None
    if r.returncode != 0:
        raise Fail(f"could not read {path}: {r.stderr.strip()}")
    return r.stdout


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def install_argv(path, mode=0o644, owner="root", group="root", src="/dev/stdin"):
    mode = f"{mode:04o}"
    return ["install", "-D", "-m", mode, "-o", owner, "-g", group, src, str(path)]


def unified(path, have, want):
    return list(
        difflib.unified_diff(
            (have or "").splitlines(),
            want.splitlines(),
            f"{path} (current)",
            f"{path} (desired)",
            lineterm="",
        )
    )


def backup(ctx, path):
    """Actions that copy `path` into this run's backup dir before it changes."""
    acts = []
    if ctx.once("backup-dir"):
        mkdir = ["install", "-d", "-m", "0700", str(ctx.backup_dir)]
        acts.append(Action(f"create backup dir {ctx.backup_dir}", mkdir))
    copy = ["cp", "-a", "--parents", str(path), str(ctx.backup_dir)]
    return [*acts, Action(f"back up {path}", copy)]


def fetch(ctx, url, dest, sha256=None):
    """Download to `dest` (kept if the hash already matches), verifying sha256."""
    if sha256 and dest.exists() and sha256_file(dest) == sha256:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, tmp.open("wb") as out:
            shutil.copyfileobj(resp, out)
        got = sha256_file(tmp)
        ctx.log.event(
            "download",
            url=url,
            path=str(dest),
            bytes=tmp.stat().st_size,
            sha256=got,
            expected_sha256=sha256,
            duration_s=round(time.monotonic() - t0, 3),
        )
        if sha256 and got != sha256:
            raise Fail(f"sha256 mismatch for {url}: got {got}, want {sha256}")
        tmp.replace(dest)
    except OSError as e:
        raise Fail(f"download failed: {url}: {e}") from e
    finally:  # never leave a partial download behind
        tmp.unlink(missing_ok=True)


def url_exists(url):
    """True/False for a clear answer, None when the server cannot be reached."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == HTTP_OK
    except urllib.error.HTTPError:
        return False
    except OSError:
        return None


# ---------------------------------------------------------------- files


class PathRes(Resource):
    """Shared by the kinds that manage one path with an owner and a mode."""

    default_mode = 0o644

    def __init__(self, step, present=True):
        super().__init__(step, present)
        self.path = Path(step["path"])
        self.target = str(self.path)
        self.mode = parse_mode(step.get("mode"), self.default_mode)
        self.owner = step.get("owner", "root")
        self.group = step.get("group", "root")
        self._exists = False

    @property
    def meta(self):
        return f"{self.owner}:{self.group} {self.mode:04o}"

    def stat(self, ctx):
        """Probe for the cases every path kind shares; else (None, meta)."""
        self._exists, meta = stat_meta(ctx, self.path)
        if self._exists is None:
            return Probe(None, UNKNOWN), meta
        if not self.present:
            have = f"present ({meta})" if self._exists else "absent"
            return Probe(not self._exists, have), meta
        if not self._exists:
            return Probe(False, "missing"), meta
        return None, meta

    def fix_meta(self):
        return [
            Action("fix owner", ["chown", f"{self.owner}:{self.group}", self.target]),
            Action("fix mode", ["chmod", f"{self.mode:04o}", self.target]),
        ]

    def write(self, content, desc="", **kwargs):
        argv = install_argv(self.path, self.mode, self.owner, self.group)
        return Action(desc or f"write {self.path}", argv, stdin=content, **kwargs)

    def remove(self, ctx):
        rm = Action(f"remove {self.path}", ["rm", "-f", self.target])
        return [*backup(ctx, self.path), rm]


@register
class FileRes(PathRes):
    """A file with exact content, owner and mode."""

    kind = "file"

    def __init__(self, step, present=True):
        super().__init__(step, present)
        self.content = step["content"]
        self._have = None
        self._same = False  # as `content`, but for the "# Managed by" lines
        self._meta_ok = True

    def probe(self, ctx):
        self._have = None
        early, meta = self.stat(ctx)
        if early:
            return early
        self._meta_ok = meta == self.meta
        self._have = read_file(ctx, self.path)
        if self._have is None:
            have = f"{meta}; content {UNKNOWN}"
            return Probe(None if self._meta_ok else False, have)
        problems = [] if self._meta_ok else ["owner/mode"]
        self._same = body(self._have) == body(self.content)
        problems += [] if self._same else ["content"]
        reason = " + ".join(problems) + " differ" if problems else ""
        return Probe(not problems, f"{meta}, {describe(self._have)}", reason)

    def plan(self, ctx, p):  # noqa: ARG002
        if not self.present:
            return self.remove(ctx)
        if not self._exists:
            diff = unified(self.path, "", self.content)
            return [self.write(self.content, detail=diff)]
        if self._have is not None and not self._same:
            diff = unified(self.path, self._have, self.content)
            return [*backup(ctx, self.path), self.write(self.content, detail=diff)]
        return self.fix_meta()


@register
class SecretRes(PathRes):
    """A root-only file holding a secret that is generated once and then kept.

    `content` holds `$secret` (default: the secret on one line); where it
    holds it more than once, every place gets the same secret.
    """

    kind = "secret"
    default_mode = 0o600

    def __init__(self, step, present=True):
        super().__init__(step, present)
        self.content = step.get("content", SECRET_MARK + "\n")
        self.nbytes = step.get("bytes", 32)
        if SECRET_MARK not in self.content:
            raise Fail(f"secret {self.path}: content must use $secret")
        self._secret = None
        self._content_ok = False
        self._meta_ok = True

    @staticmethod
    def _pattern(text):
        """Regex for `text` with the secret as group 1, the same in every place."""
        first, *rest = (re.escape(part) for part in text.split(SECRET_MARK))
        return first + r"(\S+?)" + r"\1".join(rest)

    def _find(self, text):
        """(secret already in the file or None, whether the whole file matches)."""
        whole = re.fullmatch(self._pattern(body(self.content)), body(text))
        if whole:
            return whole[1], True
        line = next(ln for ln in self.content.splitlines() if SECRET_MARK in ln)
        found = re.search(f"^{self._pattern(line)}$", text, re.MULTILINE)
        return (found[1] if found else None), False

    def probe(self, ctx):
        self._secret, self._content_ok = None, False
        early, meta = self.stat(ctx)
        if early:
            return early
        self._meta_ok = meta == self.meta
        text = read_file(ctx, self.path, secret=True)
        if text is None:
            self._content_ok = True  # unverified: only owner/mode can be fixed
            have = f"{meta}; content {UNKNOWN}"
            return Probe(None if self._meta_ok else False, have)
        self._secret, self._content_ok = self._find(text)
        ctx.log.secret(self._secret)
        have = f"{meta}, secret {'present' if self._secret else 'missing'}"
        if not self._content_ok and self._secret:
            have += ", rest of the file differs"
        return Probe(self._meta_ok and self._content_ok, have)

    def plan(self, ctx, p):  # noqa: ARG002
        if not self.present:
            return self.remove(ctx)
        if self._exists and self._content_ok:
            return self.fix_meta()
        how = "keeps the existing secret" if self._secret else "new random secret"
        secret = self._secret or secrets.token_urlsafe(self.nbytes)
        ctx.log.secret(secret)
        content = self.content.replace(SECRET_MARK, secret)
        write = self.write(content, f"write {self.path} ({how})", secret=True)
        return [*(backup(ctx, self.path) if self._exists else []), write]


@register
class DirRes(PathRes):
    """A directory; when absent is wanted it is removed only if empty.

    `remove_empty_tree = true` also removes it when all it holds is empty
    directories, empty files and symlinks (e.g. the mount points systemd
    leaves in a service's RootDirectory): nothing with data in it.
    """

    kind = "dir"
    default_mode = 0o755

    def __init__(self, step, present=True):
        super().__init__(step, present)
        self.empty_tree = step.get("remove_empty_tree", False)

    def _has_data(self, ctx):
        """A file with content that stays after this run; "" if none, None: sudo."""
        find = ["find", self.target, "-xdev", "-type", "f", "-size", "+0"]
        for key in sorted(ctx.changing):  # what this run removes does not count
            if Path(key.partition(":")[2]).is_relative_to(self.path):
                find += ["!", "-path", key.partition(":")[2]]
        find += ["-print", "-quit"]
        r = ctx.sh.probe(find)
        if r.returncode != 0:  # a subdirectory only root can read
            r = ctx.sh.probe(find, sudo=True)
        return None if r is None else r.stdout.strip()

    def _removable_tree(self, ctx, meta):
        data = self._has_data(ctx)
        if data is None:
            return Probe(None, UNKNOWN)
        if data:
            return Probe(True, f"holds data, kept ({data})")
        return Probe(False, f"present, holds no data ({meta})")

    def _removable(self, ctx, meta):
        """Absent is wanted: only once nothing but what this run removes is left."""
        going = {k.partition(":")[2] for k in ctx.changing}
        try:
            left = [p for p in self.path.iterdir() if str(p) not in going]
        except PermissionError:
            return Probe(None, UNKNOWN)
        except NotADirectoryError:
            return Probe(True, f"not a directory, kept ({meta})")
        if left:
            return Probe(True, f"not empty, kept ({meta})")
        return Probe(False, f"present, empty after this run ({meta})")

    def probe(self, ctx):
        early, meta = self.stat(ctx)
        if not self.present and self._exists:
            tree = self.empty_tree and self.path.is_dir()
            return (self._removable_tree if tree else self._removable)(ctx, meta)
        if early:
            return early
        if not self.path.is_dir():
            return Probe(False, f"not a directory ({meta})")
        return Probe(meta == self.meta, meta)

    def plan(self, ctx, p):  # noqa: ARG002
        if not self.present and self.empty_tree:
            find = ["find", self.target, "-xdev", "-depth", "("]
            find += ["-type", "f", "-empty", "-o", "-type", "l"]
            find += ["-o", "-type", "d", "-empty", ")", "-delete"]
            return [Action(f"remove {self.path} (it holds no data)", find)]
        if not self.present:
            return [Action(f"remove empty dir {self.path}", ["rmdir", self.target])]
        argv = ["install", "-d", "-m", f"{self.mode:04o}"]
        argv += ["-o", self.owner, "-g", self.group, self.target]
        return [Action(f"create {self.path}", argv)]


@register
class JsonMergeRes(PathRes):
    """Keys merged into a JSON file, leaving every other key as it is."""

    kind = "json_merge"

    def __init__(self, step, present=True):
        super().__init__(step, present)
        self.data = step["data"]
        self._have_text = ""
        self._want = {}

    def probe(self, ctx):
        self._exists, meta = stat_meta(ctx, self.path)
        text = read_file(ctx, self.path) if self._exists else ""
        if self._exists is None or text is None:
            return Probe(None, UNKNOWN)
        try:
            have = json.loads(text) if text.strip() else {}
        except ValueError as e:
            raise Fail(f"{self.path} is not valid JSON: {e}") from e
        merge = json_merge if self.present else json_unmerge
        self._have_text, self._want = text, merge(have, self.data)
        good = self._want == have and (self._exists or not self.present)
        return Probe(good, meta if good or not self._exists else "keys differ")

    def plan(self, ctx, p):  # noqa: ARG002
        want = json.dumps(self._want, indent=4) + "\n"
        diff = unified(self.path, self._have_text, want)
        saved = backup(ctx, self.path) if self._exists else []
        return [*saved, self.write(want, detail=diff)]


def json_merge(have, ours):
    out = dict(have)
    for k, v in ours.items():
        both = isinstance(v, dict) and isinstance(out.get(k), dict)
        out[k] = json_merge(out[k], v) if both else v
    return out


def json_unmerge(have, ours):
    """Remove what json_merge added: our leaves, and tables left empty."""
    out = dict(have)
    for k, v in ours.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = json_unmerge(out[k], v)
            if not out[k]:
                del out[k]
        elif k in out and out[k] == v:
            del out[k]
    return out


@register
class DownloadRes(PathRes):
    """A pinned download: a file, or with `extract` an unpacked archive.

    `link` keeps a symlink pointing at it (e.g. /opt/tool/current).
    """

    kind = "download"

    def __init__(self, step, present=True):
        super().__init__(step, present)
        self.url, self.sha256 = step["url"], step["sha256"]
        self.extract = step.get("extract", False)
        self.strip = step.get("strip", 0)
        self.link = Path(step["link"]) if step.get("link") else None
        self._ours = False
        self._meta_ok = True
        self._link_ok = True

    def _installed(self, ctx):
        """Whether the path holds this download; None while that needs sudo."""
        check = self.path / MARKER if self.extract else self.path
        if not os.access(check, os.R_OK):
            exists, _ = stat_meta(ctx, check)
            return None if exists is None or exists else False
        if self.extract:
            return check.read_text().strip() == self.sha256
        return sha256_file(check) == self.sha256

    def probe(self, ctx):
        self._ours = self._installed(ctx)
        self._exists, meta = stat_meta(ctx, self.path)
        linked = self.link is not None and self.link.is_symlink()
        self._link_ok = self.link is None or (
            linked and str(self.link.readlink()) == self.target
        )
        if self._ours is None or self._exists is None:
            return Probe(None, UNKNOWN)
        if not self.present:
            if self._exists and not self._ours:
                return Probe(True, "not this download, kept")
            return Probe(not self._exists and not linked, meta)
        if not self._ours:
            return Probe(False, "other content" if self._exists else "missing")
        self._meta_ok = self.extract or meta == self.meta
        problems = ([] if self._meta_ok else ["owner/mode"]) + (
            [] if self._link_ok else [f"{self.link} does not point to it"]
        )
        return Probe(not problems, meta, ", ".join(problems))

    def plan(self, ctx, p):  # noqa: ARG002
        if not self.present:
            rm = ["rm", "-rf" if self.extract else "-f", self.target]
            acts = [Action(f"remove {self.path}", rm)] if self._exists else []
            if self.link is not None and self.link.is_symlink():
                acts.append(Action(f"remove {self.link}", ["rm", "-f", str(self.link)]))
            return acts
        acts = []
        if not self._ours:
            cached = ctx.cache_dir / Path(self.url).name
            short = self.sha256[:12]
            acts.append(
                Action(
                    f"download {self.url} -> {cached} (sha256 {short}…)",
                    fn=lambda: fetch(ctx, self.url, cached, self.sha256),
                )
            )
            acts += self._unpack(cached) if self.extract else [self._copy(cached)]
        elif not self._meta_ok:
            acts += self.fix_meta()
        if not self._link_ok:
            ln = ["ln", "-sfn", self.target, str(self.link)]
            acts.append(Action(f"link {self.link}", ln))
        return acts

    def _copy(self, cached):
        argv = install_argv(self.path, self.mode, self.owner, self.group, str(cached))
        return Action(f"install {self.path}", argv)

    def _unpack(self, cached):
        tar = ["tar", "-xf", str(cached), "-C", self.target, "--no-same-owner"]
        tar += [f"--strip-components={self.strip}"] if self.strip else []
        mark = install_argv(self.path / MARKER)
        return [
            Action(f"create {self.path}", ["install", "-d", "-m", "0755", self.target]),
            Action(f"unpack into {self.path}", tar),
            Action(f"mark {self.path}", mark, stdin=self.sha256 + "\n"),
        ]


# ---------------------------------------------------------------- packages


def apt_lists_stale():
    times = [p.stat().st_mtime for p in APT_STAMPS if p.exists()]
    return not times or time.time() - max(times) > APT_MAX_AGE


def policy_rc_is_ours():
    """True: our guard (left by an interrupted run); False: foreign; None: absent."""
    try:
        return POLICY_RC.read_text() == POLICY_RC_TEXT
    except FileNotFoundError:
        return None
    except OSError:
        return False


def guarded_install(sh, argv):
    """Install with service starts blocked; the guard is always removed again."""
    try:
        guard = ["install", "-m", "0755", "/dev/stdin", str(POLICY_RC)]
        sh.run(guard, stdin=POLICY_RC_TEXT)
        sh.run(argv)
    finally:
        sh.run(["rm", "-f", str(POLICY_RC)], check=False)


@register
class AptRes(Resource):
    """Distribution packages; `no_autostart` keeps their services from starting."""

    kind = "apt"

    def __init__(self, step, present=True):
        super().__init__(step, present)
        self.packages = list(step["packages"])
        self.no_autostart = step.get("no_autostart", False)
        self.recommends = step.get("recommends", False)
        self.target = " ".join(self.packages)
        self._installed = []
        self._stale_guard = False

    def probe(self, ctx):
        query = ["dpkg-query", "-W", "-f=${Package} ${db:Status-Status}\n"]
        lines = ctx.sh.probe([*query, *self.packages]).stdout.splitlines()
        found = {ln.split()[0] for ln in lines if ln.endswith(" installed")}
        self._installed = [p for p in self.packages if p in found]
        missing = [p for p in self.packages if p not in found]
        have = f"missing: {' '.join(missing)}" if missing else "installed"
        if not self.present:
            have = f"installed: {' '.join(self._installed)}"
            return Probe(not self._installed, have if self._installed else "absent")
        self._stale_guard = self.no_autostart and policy_rc_is_ours() is True
        if self._stale_guard:
            reason = f"{POLICY_RC} was left behind by an interrupted install"
            return Probe(False, have, reason)
        return Probe(not missing, have)

    def plan(self, ctx, p):  # noqa: ARG002
        if not self.present:
            remove = ["apt-get", "remove", "-y", *self._installed]
            return [Action("remove packages", remove)]
        missing = [pkg for pkg in self.packages if pkg not in self._installed]
        acts = []
        if self._stale_guard:
            rm = ["rm", "-f", str(POLICY_RC)]
            acts.append(Action("remove stale policy-rc.d", rm))
        if not missing:
            return acts
        if self.no_autostart and policy_rc_is_ours() is False:
            raise Fail(f"{POLICY_RC} exists and is not ours; not touching it")
        new_source = any(k.startswith("apt_source:") for k in ctx.changing)
        if (apt_lists_stale() or new_source) and ctx.once("apt-update"):
            acts.append(Action("refresh package lists", ["apt-get", "update"]))
        argv = ["env", "DEBIAN_FRONTEND=noninteractive", "UCF_FORCE_CONFFOLD=1"]
        argv += ["apt-get", "install", "-y", "-q"]
        argv += [] if self.recommends else ["--no-install-recommends"]
        argv += ["-o", "Dpkg::Progress-Fancy=0", "-o", "Dpkg::Use-Pty=0", *missing]
        if not self.no_autostart:
            return [*acts, Action("install packages", argv)]
        desc = f"{shlex.join(ctx.sh.argv(argv, sudo=True))}  (services blocked)"
        return [*acts, Action(desc, fn=lambda: guarded_install(ctx.sh, argv))]


@register
class AptSourceRes(Resource):
    """A deb822 source in sources.list.d, signed by its own keyring.

    Recognises a source the release upgrader disabled (`Enabled: no`) or left
    on the old suite, and re-enables it once the vendor publishes the suite.
    """

    kind = "apt_source"

    def __init__(self, step, present=True):
        super().__init__(step, present)
        name = step["name"]
        self.target = name
        self.path = APT_SOURCES / f"{name}.sources"
        self.key_url = step["key_url"]
        self.key_sha256 = step.get("key_sha256")
        ext = ".gpg" if self.key_url.endswith(".gpg") else ".asc"
        self.keyring = APT_KEYRINGS / f"{name}{ext}"
        uris = step["uris"]
        self.uris = [uris] if isinstance(uris, str) else list(uris)
        self.suites = list(step["suites"])
        fields = {
            "Types": "deb",
            "URIs": " ".join(self.uris),
            "Suites": " ".join(self.suites),
            "Components": " ".join(step.get("components", ["main"])),
            "Architectures": " ".join(step.get("architectures", [])),
            "Signed-By": str(self.keyring),
        }
        lines = [f"{k}: {v}" for k, v in fields.items() if v]
        self.content = "# Managed by sux install.\n" + "\n".join(lines) + "\n"
        self._have = None
        self._key_ok = False

    def _unpublished(self):
        """First suite the vendor does not serve (yet), as (url, reachable)."""
        for uri in self.uris:
            for suite in self.suites:
                url = f"{uri.rstrip('/')}/dists/{suite}/InRelease"
                found = url_exists(url)
                if not found:
                    return url, found is not None
        return None

    def probe(self, ctx):  # noqa: ARG002
        self._have = self.path.read_text() if self.path.exists() else None
        self._key_ok = self.keyring.exists() and (
            not self.key_sha256 or sha256_file(self.keyring) == self.key_sha256
        )
        if not self.present:
            gone = self._have is None and not self.keyring.exists()
            return Probe(gone, "absent" if gone else "present")
        if self._have is None:
            return Probe(False, "missing")
        if self._have == self.content:
            return Probe(self._key_ok, "enabled" if self._key_ok else "keyring differs")
        flags = re.MULTILINE | re.IGNORECASE
        disabled = re.search(r"^Enabled:\s*no\s*$", self._have, flags)
        have = "disabled (release upgrade?)" if disabled else "differs"
        missing = self._unpublished()
        if missing:
            url, reachable = missing
            why = "is not published yet" if reachable else "could not be reached"
            return Probe(True, f"{have}; left as it is: {url} {why}")
        return Probe(False, have)

    def plan(self, ctx, p):  # noqa: ARG002
        if not self.present:
            saved = backup(ctx, self.path) if self._have is not None else []
            rm = ["rm", "-f", str(self.path), str(self.keyring)]
            return [*saved, Action(f"remove apt source {self.target}", rm)]
        acts = []
        if not self._key_ok:
            cached = ctx.cache_dir / self.keyring.name
            acts += [
                Action(
                    f"download {self.key_url} -> {cached}",
                    fn=lambda: fetch(ctx, self.key_url, cached, self.key_sha256),
                ),
                Action("install keyring", install_argv(self.keyring, src=str(cached))),
            ]
        if self._have != self.content:
            acts += backup(ctx, self.path) if self._have is not None else []
            diff = unified(self.path, self._have, self.content)
            write = install_argv(self.path)
            acts.append(
                Action(f"write {self.path}", write, stdin=self.content, detail=diff)
            )
        return acts


@register
class DebRes(Resource):
    """One pinned .deb from a URL, verified by sha256 before it is installed."""

    kind = "deb"

    def __init__(self, step, present=True):
        super().__init__(step, present)
        self.name = self.target = step["name"]
        self.url, self.sha256 = step["url"], step["sha256"]
        self.version = str(step["version"])

    def probe(self, ctx):
        query = ["dpkg-query", "-W", "-f=${Version}\t${db:Status-Status}", self.name]
        r = ctx.sh.probe(query)
        version, _, status = r.stdout.partition("\t")
        installed = r.returncode == 0 and status.strip() == "installed"
        have = f"installed {version}" if installed else "not installed"
        if not self.present:
            return Probe(not installed, have)
        pinned = rf"(\d+:)?{re.escape(self.version)}(-\d+)?"
        return Probe(installed and bool(re.fullmatch(pinned, version)), have)

    def plan(self, ctx, p):  # noqa: ARG002
        if not self.present:
            return [Action(f"remove package {self.name}", ["dpkg", "-r", self.name])]
        deb = ctx.cache_dir / Path(self.url).name
        short = self.sha256[:12]
        return [
            Action(
                f"download {self.url} -> {deb} (sha256 {short}…)",
                fn=lambda: fetch(ctx, self.url, deb, self.sha256),
            ),
            Action(f"install package {self.name}", ["dpkg", "-i", str(deb)]),
        ]


# ---------------------------------------------------------------- system


def active_since(sh, unit):
    show = ["systemctl", "show", unit, "-p", "ActiveEnterTimestamp"]
    v = sh.out([*show, "--timestamp=us+utc", "--value"])
    if not v:
        return None
    parsed = datetime.strptime(v, "%a %Y-%m-%d %H:%M:%S.%f %Z")  # noqa: DTZ007
    return parsed.replace(tzinfo=UTC).timestamp()


def changed_at(path):
    try:
        return Path(path).stat().st_ctime
    except OSError:
        return None


@register
class UnitRes(Resource):
    """A systemd unit's state.

    running: enabled, active, and started after everything in `restart_on`
    last changed. on-demand: left to socket/timer activation, restarted only
    if it runs with stale inputs. stopped: disabled and inactive. masked.
    `restart_on` takes paths and resource keys (e.g. "deb:name").
    """

    kind = "unit"

    def __init__(self, step, present=True):
        super().__init__(step, present)
        self.name = self.target = step["name"]
        self.state = step.get("state", "running")
        if self.state not in UNIT_STATES:
            raise Fail(f"unit {self.name}: state must be one of {UNIT_STATES}")
        self.restart_on = list(step.get("restart_on", []))
        self.validate = step.get("validate")
        self._en = self._act = ""

    def label(self):
        state = self.state if self.present else f"not {self.state}"
        return f"unit {self.name}  ({state})"

    def _stale(self, ctx):
        since = active_since(ctx.sh, self.name) if self._act == "active" else None
        stale = [
            Path(p).name
            for p in self.restart_on
            if since and (changed_at(p) or 0) > since
        ]
        pending = sorted(
            k
            for k in ctx.changing
            if k in self.restart_on or k.partition(":")[2] in self.restart_on
        )
        reasons = [f"changed after start: {', '.join(stale)}"] if stale else []
        reasons += [f"changes in this run: {', '.join(pending)}"] if pending else []
        return "; ".join(reasons)

    def probe(self, ctx):
        self._en = ctx.sh.out(["systemctl", "is-enabled", self.name]) or "not-found"
        self._act = ctx.sh.out(["systemctl", "is-active", self.name]) or "inactive"
        en, act = self._en, self._act
        have = f"{en}, {act}"
        off = en != "enabled" and act != "active"
        if self.state == "masked":
            return Probe((en == "masked") == self.present, have)
        if not self.present or self.state == "stopped":
            return Probe(off, have)
        reason = self._stale(ctx)
        if self.state == "on-demand":
            return Probe(not (act == "active" and reason), have, reason)
        return Probe(en in ENABLED and act == "active" and not reason, have, reason)

    def plan(self, ctx, p):  # noqa: ARG002
        name = self.name
        if self.state == "masked":
            verb = ["mask", "--now"] if self.present else ["unmask"]
            return [Action(f"{verb[0]} {name}", ["systemctl", *verb, name])]
        if not self.present or self.state == "stopped":
            stop = ["systemctl", "disable", "--now", name]
            return [Action(f"stop + disable {name}", stop, check=False)]
        acts = []
        if self.state == "running" and self._en not in ENABLED:
            acts.append(Action(f"enable {name}", ["systemctl", "enable", name]))
        if self._act != "active" or p.reason:
            if self.validate:  # a config the service rejects never gets loaded
                desc = f"validate before restarting {name}"
                acts.append(Action(desc, self.validate, sudo=False))
            acts.append(Action(f"(re)start {name}", ["systemctl", "restart", name]))
        return acts


def unit_for(path):
    """Unit that a file under the systemd unit dir belongs to, else None."""
    path = Path(path)
    if UNIT_DIR not in path.parents:
        return None
    return path.relative_to(UNIT_DIR).parts[0].removesuffix(".d")  # drop-ins


class DaemonReloadRes(Resource):
    """Added by the engine around unit steps; not a recipe step kind."""

    kind = "systemd"

    def __init__(self, step, present=True):
        super().__init__(step, present)
        self.units = step["units"]
        self.target = step.get("label", "daemon-reload")

    def label(self):
        return "systemd daemon-reload"

    def probe(self, ctx):
        show = ["systemctl", "show", "-p", "NeedDaemonReload", "--value", *self.units]
        need = "yes" in ctx.sh.out(show).split() and ctx.once("reload-needed")
        pending = {k for k in ctx.changing if unit_for(k.partition(":")[2])}
        pending = {k for k in pending if ctx.once(f"reload:{k}")}
        if pending:
            return Probe(False, "unit files change in this run")
        return Probe(not need, "reload needed" if need else "up to date")

    def plan(self, ctx, p):  # noqa: ARG002
        return [Action("reload systemd", ["systemctl", "daemon-reload"])]


@register
class GroupMemberRes(Resource):
    """Membership of a user in a group (takes effect at the next login)."""

    kind = "group_member"

    def __init__(self, step, present=True):
        super().__init__(step, present)
        self.user, self.group = step["user"], step["group"]
        self.target = f"{self.user} in {self.group}"

    def probe(self, ctx):
        r = ctx.sh.probe(["id", "-nG", self.user])
        if r.returncode != 0:
            raise Fail(f"no such user: {self.user}")
        member = self.group in r.stdout.split()
        return Probe(member == self.present, "member" if member else "not a member")

    def plan(self, ctx, p):  # noqa: ARG002
        flag = "-a" if self.present else "-d"
        verb = "add" if self.present else "remove"
        desc = f"{verb} {self.user}: group {self.group} (applies at next login)"
        return [Action(desc, ["gpasswd", flag, self.user, self.group])]


# ---------------------------------------------------------------- libvirt

VIRSH = ["virsh", "-c", "qemu:///system"]
# Read-only connection: answers any user, without the libvirt group or sudo.
VIRSH_RO = ["virsh", "-r", "-c", "qemu:///system"]
DOMAIN_NS = "urn:sux:install"  # of the element that records what was defined
ET.register_namespace("sux", DOMAIN_NS)


def virsh_xml(ctx, *args):
    """Parsed XML from a read-only virsh query; None when it has no answer."""
    r = ctx.sh.probe([*VIRSH_RO, *args])
    try:
        return ET.fromstring(r.stdout) if r.returncode == 0 else None
    except ET.ParseError:
        return None


@register
class LibvirtDomainRes(Resource):
    """A libvirt domain, defined from `content` (its XML).

    libvirt rewrites what it is given, so the definition cannot be compared
    with the XML itself: the sha256 of `content` is stored in the domain's
    <metadata>, and the domain is defined again only when that differs. A
    change made by other means (virt-manager, virsh edit) is therefore left
    alone until `content` changes. A redefinition keeps the domain's UUID.

    Absent: undefined, keeping NVRAM and TPM state unless `purge` is set.
    Disks are never touched here. A running domain is not undefined.
    """

    kind = "libvirt_domain"

    def __init__(self, step, present=True):
        super().__init__(step, present)
        self.name = self.target = step["name"]
        self.content = step["content"]
        self.purge = bool(step.get("purge", False))
        self.sha256 = hashlib.sha256(self.content.encode()).hexdigest()
        self._have = None  # the current definition, parsed
        self._state = ""

    def probe(self, ctx):
        self._have = virsh_xml(ctx, "dumpxml", "--inactive", self.name)
        if self._have is None:
            return Probe(not self.present, "not defined")
        self._state = ctx.sh.out([*VIRSH_RO, "domstate", self.name]) or "unknown"
        have = f"defined ({self._state})"
        if not self.present:
            if self._state != "shut off":
                stop = shlex.join([*VIRSH, "shutdown", self.name])
                raise Fail(f"domain {self.name} is {self._state}; stop it: {stop}")
            return Probe(False, have)
        mark = self._have.find(f"metadata/{{{DOMAIN_NS}}}recipe")
        same = mark is not None and mark.get("sha256") == self.sha256
        return Probe(same, have, "" if same else "definition differs")

    def xml(self):
        """`content` with the hash in <metadata> and the current UUID."""
        try:
            root = ET.fromstring(self.content)
        except ET.ParseError as e:
            raise Fail(f"domain {self.name}: its XML does not parse: {e}") from e
        meta = root.find("metadata")
        if meta is None:
            meta = ET.SubElement(root, "metadata")
        ET.SubElement(meta, f"{{{DOMAIN_NS}}}recipe", sha256=self.sha256)
        uuid = None if self._have is None else self._have.findtext("uuid")
        if uuid and root.find("uuid") is None:
            ET.SubElement(root, "uuid").text = uuid
        return ET.tostring(root, encoding="unicode") + "\n"

    def _save(self, ctx):
        """Keep the definition that is about to go in this run's backup dir."""
        old = ET.tostring(self._have, encoding="unicode") + "\n"
        path = ctx.backup_dir / f"domain-{self.name}.xml"
        desc = f"back up the definition of {self.name}"
        return Action(desc, install_argv(path, 0o600), stdin=old)

    def plan(self, ctx, p):  # noqa: ARG002
        saved = [] if self._have is None else [self._save(ctx)]
        if not self.present:
            keep = "" if self.purge else "keep-"
            flags = [f"--{keep}nvram", f"--{keep}tpm", "--managed-save"]
            flags += ["--snapshots-metadata", "--checkpoints-metadata"]
            undefine = [*VIRSH, "undefine", self.name, *flags]
            return [*saved, Action(f"undefine domain {self.name}", undefine)]
        define = [*VIRSH, "define", "/dev/stdin"]
        return [*saved, Action(f"define domain {self.name}", define, stdin=self.xml())]


def network_flags(ctx, network):
    """`virsh net-info` as a dict, e.g. {"Active": "yes", "Autostart": "no"}."""
    info = ctx.sh.out([*VIRSH_RO, "net-info", network])
    return dict(re.findall(r"^(\w+):\s+(\S+)", info, re.MULTILINE))


@register
class LibvirtNetworkRes(Resource):
    """A libvirt network that exists is kept started and autostarted.

    It is never defined or removed here: the distribution's package brings
    `default`, and other guests may use it.
    """

    kind = "libvirt_network"

    def __init__(self, step, present=True):
        super().__init__(step, present)
        self.name = self.target = step.get("name", "default")
        self._flags = {}

    def probe(self, ctx):
        if not self.present:
            return Probe(True, "left as it is")
        self._flags = network_flags(ctx, self.name)
        if not self._flags:
            return Probe(False, "not there (yet)")
        want = ("Active", "Autostart")
        off = [k.lower() for k in want if self._flags.get(k) != "yes"]
        have = f"not {', not '.join(off)}" if off else "active, autostart"
        return Probe(not off, have)

    def plan(self, ctx, p):  # noqa: ARG002
        acts = []
        if self._flags.get("Autostart") != "yes":
            auto = [*VIRSH, "net-autostart", self.name]
            acts.append(Action(f"autostart network {self.name}", auto))
        if self._flags.get("Active") != "yes":
            # not there yet: its package may have started it by the time this runs
            start = [*VIRSH, "net-start", self.name]
            known = bool(self._flags)
            acts.append(Action(f"start network {self.name}", start, check=known))
        return acts


@register
class LibvirtDhcpHostRes(Resource):
    """A fixed address for one MAC on a libvirt network (a DHCP reservation).

    Added with `net-update`, which does not interrupt the network or the
    guests on it.
    """

    kind = "libvirt_dhcp_host"

    def __init__(self, step, present=True):
        super().__init__(step, present)
        self.network = step.get("network", "default")
        self.mac, self.ip = step["mac"].lower(), step["ip"]
        self.name = step.get("name", "")
        self.target = f"{self.network} {self.mac} -> {self.ip}"
        self._hosts = []  # (mac, ip) reserved now
        self._active = True

    def probe(self, ctx):
        net = virsh_xml(ctx, "net-dumpxml", self.network)
        if net is None:
            return Probe(not self.present, f"no network {self.network} (yet)")
        self._active = network_flags(ctx, self.network).get("Active") == "yes"
        hosts = net.findall("ip/dhcp/host")
        self._hosts = [(h.get("mac", "").lower(), h.get("ip")) for h in hosts]
        reserved = (self.mac, self.ip) in self._hosts
        if not self.present:
            return Probe(not reserved, "reserved" if reserved else "absent")
        ranges = [(ip.get("address"), ip.get("netmask")) for ip in net.findall("ip")]
        nets = [
            ipaddress.ip_network(f"{address}/{mask}", strict=False)
            for address, mask in ranges
            if address and mask
        ]
        if not any(ipaddress.ip_address(self.ip) in n for n in nets):
            have = ", ".join(str(n) for n in nets) or "no address range"
            raise Fail(f"{self.ip} is not on network {self.network} ({have})")
        taken = [mac for mac, ip in self._hosts if ip == self.ip and mac != self.mac]
        if taken:
            raise Fail(f"{self.ip} is reserved for {taken[0]} on {self.network}")
        return Probe(reserved, "reserved" if reserved else "not reserved")

    def _update(self, verb, mac, ip):
        name = f" name='{self.name}'" if verb == "add" and self.name else ""
        host = f"<host mac='{mac}'{name} ip='{ip}'/>"
        argv = [*VIRSH, "net-update", self.network, verb, "ip-dhcp-host", host]
        argv += ["--live"] if self._active else []
        return Action(f"{verb} reservation {mac} -> {ip}", [*argv, "--config"])

    def plan(self, ctx, p):  # noqa: ARG002
        if not self.present:
            return [self._update("delete", self.mac, self.ip)]
        stale = [self._update("delete", m, i) for m, i in self._hosts if m == self.mac]
        return [*stale, self._update("add", self.mac, self.ip)]


@register
class ImageRes(PathRes):
    """A disk image: an empty qcow2 of `size`, or an ISO of the files in `source`.

    A qcow2 is created once and then left alone, whatever its size, owner and
    mode become (libvirt changes the owner while a guest uses it); it holds
    data, so absent removes it only with `purge`. An ISO is built again when
    a file in `source` is newer than it or changes in this run.
    """

    kind = "image"
    default_mode = 0o600

    def __init__(self, step, present=True):
        super().__init__(step, present)
        self.source = Path(step["source"]) if step.get("source") else None
        self.size = str(step.get("size", ""))
        self.volume = step.get("label", "DATA")
        self.purge = bool(step.get("purge", False))
        if bool(self.source) == bool(self.size):
            raise Fail(f"image {self.path}: give `size` (qcow2) or `source` (ISO)")

    def _stale(self, ctx):
        """Why the ISO has to be built again, or ""."""
        changing = (Path(key.partition(":")[2]) for key in ctx.changing)
        if any(self.source in path.parents for path in changing):
            return "its files change in this run"
        built = self.path.lstat().st_mtime
        try:
            files = [f for f in self.source.rglob("*") if f.is_file()]
        except OSError:
            files = []
        newer = [f.name for f in files if f.lstat().st_mtime > built]
        return f"older than {', '.join(sorted(newer))}" if newer else ""

    def probe(self, ctx):
        early, meta = self.stat(ctx)
        if not self.present and self._exists and not self.source and not self.purge:
            return Probe(True, "kept: it holds data (removed only with purge)")
        if early:
            return early
        reason = self._stale(ctx) if self.source else ""
        return Probe(not reason, f"present ({meta})", reason)

    def plan(self, ctx, p):  # noqa: ARG002
        if not self.present:
            return [Action(f"remove {self.path}", ["rm", "-f", self.target])]
        if not self.source:
            create = ["qemu-img", "create", "-f", "qcow2", self.target, self.size]
            desc = f"create {self.path} ({self.size}, grows as it fills)"
            return [Action(desc, create), *self.fix_meta()]
        iso = ["xorriso", "-as", "mkisofs", "-quiet", "-J", "-r", "-V", self.volume]
        iso += ["-o", self.target, str(self.source)]
        return [Action(f"build {self.path} from {self.source}", iso), *self.fix_meta()]


@register
class PythonRes(Resource):
    """Whatever will not reduce to data: `function` in the recipe's hooks.py.

    The hook is called as fn(ctx, step, present) and returns a Probe, or
    (Probe, [Action, ...]). Raising Fail there stops the run before any prompt.
    """

    kind = "python"

    def __init__(self, step, present=True):
        super().__init__(step, present)
        self.function = step["function"]
        self.target = step.get("label", self.function)
        self._actions = []

    def probe(self, ctx):
        fn = ctx.hooks.get(self.function)
        if fn is None:
            raise Fail(f"hooks.py has no function {self.function!r}")
        result = fn(ctx, self.step, self.present)
        probe, actions = result if isinstance(result, tuple) else (result, [])
        self._actions = list(actions)
        return probe

    def plan(self, ctx, p):  # noqa: ARG002
        return self._actions
