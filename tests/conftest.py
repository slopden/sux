"""Fixtures for the install engine tests: a scripted Shell and a fake host.

Nothing here starts a process or touches a path outside pytest's tmp dirs.
"""

import grp
import os
import pwd
import shlex
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from sux.install import model, resources
from sux.install.log import RunLog
from sux.install.shell import Shell

FIXTURES = Path(__file__).parent / "fixtures"
DEMO = FIXTURES / "recipes" / "demo"
SITES = FIXTURES / "sites"  # a recipe that manages a list (kept out of recipes/)


class FakeShell(Shell):
    """A Shell whose processes are scripted.

    `replies` maps a command prefix (without sudo) to (rc, stdout) or to a
    callable(cmd, stdin) returning that. Commands the resources use to change
    things (install, rm, systemctl ...) are emulated on real files under
    tmp_path and on `units`, so apply -> re-probe converges like a real host.
    """

    def __init__(self, log=None):
        super().__init__(log)
        self.is_root = False
        self.sudo_probes = False
        self.sudo_ok = True
        self.replies = {}
        self.spawned = []  # every argv, as started
        self.envs = []  # the extra environment of each, None if it had none
        self.units = {}  # name -> {"enabled": str, "active": bool, "since": float}
        self.need_reload = False

    # -- helpers for tests

    def commands(self, prefix=""):
        """Spawned commands (sudo stripped) as strings, filtered by prefix."""
        lines = [shlex.join(a[1:] if a[0] == "sudo" else a) for a in self.spawned]
        return [ln for ln in lines if ln.startswith(prefix)]

    def changes(self):
        """Commands that ran through sudo (probes via sudo are cat/stat)."""
        return [
            shlex.join(a[1:])
            for a in self.spawned
            if a[0] == "sudo" and a[1] not in ("-v", "-n", "cat", "stat")
        ]

    def unit(self, name, enabled="enabled", active=True, since=None):
        since = time.time() if since is None else since
        self.units[name] = {"enabled": enabled, "active": active, "since": since}

    # -- the process boundary

    def spawn(self, argv, stdin=None, interactive=False, env=None):  # noqa: ARG002
        self.spawned.append(list(argv))
        self.envs.append(env)
        cmd = list(argv[1:]) if argv[0] == "sudo" else list(argv)
        cmd = cmd[1:] if cmd[0] == "-n" else cmd  # sudo -n: read-only, no prompt
        line = shlex.join(cmd)
        for prefix, reply in self.replies.items():
            if line.startswith(prefix):
                rc, out = reply(cmd, stdin) if callable(reply) else reply
                return subprocess.CompletedProcess(argv, rc, out, "")
        rc, out = self.emulate(cmd, stdin)
        return subprocess.CompletedProcess(argv, rc, out, "")

    def emulate(self, cmd, stdin):  # noqa: C901, PLR0911, PLR0912
        tool, args = cmd[0], cmd[1:]
        if tool == "-v":  # sudo -v
            return (0 if self.sudo_ok else 1), ""
        if tool == "install" and "-d" in args:
            path = Path(args[-1])
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(int(args[args.index("-m") + 1], 8))
        elif tool == "install":
            src, dest = args[-2], Path(args[-1])
            dest.parent.mkdir(parents=True, exist_ok=True)
            data = stdin.encode() if src == "/dev/stdin" else Path(src).read_bytes()
            if dest.exists():
                dest.chmod(0o600)
            dest.write_bytes(data)
            dest.chmod(int(args[args.index("-m") + 1], 8))
        elif tool == "rm":
            for path in (Path(a) for a in args if not a.startswith("-")):
                path.unlink(missing_ok=True)
        elif tool == "rmdir":
            Path(args[0]).rmdir()
        elif tool == "touch":
            Path(args[0]).touch()
        elif tool == "chmod":
            Path(args[1]).chmod(int(args[0], 8))
        elif tool == "cat":
            return 0, Path(args[0]).read_text()
        elif tool == "find":
            skip = {args[i + 1] for i, a in enumerate(args) if a == "-path"}
            return self.find(Path(args[0]), skip, delete="-delete" in args)
        elif tool == "systemctl":
            return self.systemctl(args)
        return 0, ""

    @staticmethod
    def find(top, skip, delete):
        """The two forms the dir kind uses: files with content; prune what has none."""
        if not delete:
            files = [p for p in top.rglob("*") if p.is_file() and str(p) not in skip]
            full = [p for p in files if not p.is_symlink() and p.stat().st_size]
            return 0, "".join(f"{p}\n" for p in full[:1])
        for path in sorted(top.rglob("*"), reverse=True):  # deepest first
            if path.is_symlink() or (path.is_file() and not path.stat().st_size):
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        if not any(top.iterdir()):
            top.rmdir()
        return 0, ""

    def systemctl(self, args):  # noqa: C901, PLR0911
        verb = args[0]
        name = args[1] if verb == "show" else args[-1]
        unit = self.units.get(name)
        if verb == "status":
            return 0, f"status of {' '.join(a for a in args[1:] if a[0] != '-')}"
        if verb == "is-enabled":
            return (0, unit["enabled"]) if unit else (4, "")
        if verb == "is-active":
            return (0, "active") if unit and unit["active"] else (3, "inactive")
        if verb == "show" and "NeedDaemonReload" in args:
            return 0, "yes" if self.need_reload else "no"
        if verb == "show":
            if not (unit and unit["active"]):
                return 0, ""
            since = datetime.fromtimestamp(unit["since"], UTC)
            return 0, since.strftime("%a %Y-%m-%d %H:%M:%S.%f UTC")
        if verb == "daemon-reload":
            self.need_reload = False
            return 0, ""
        unit = self.units.setdefault(
            name, {"enabled": "disabled", "active": False, "since": 0}
        )
        if verb == "enable":
            unit["enabled"] = "enabled"
        elif verb == "restart":
            unit.update(active=True, since=time.time())
        elif verb == "disable":
            unit.update(enabled="disabled", active=False)
        elif verb == "mask":
            unit.update(enabled="masked", active=False)
        elif verb == "unmask":
            unit["enabled"] = "disabled"
        return 0, ""


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point every host path the engine knows at tmp_path."""
    host = tmp_path / "host"
    for name in ("config", "state", "home", "units", "sources", "keyrings"):
        (host / name).mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(host / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(host / "state"))
    monkeypatch.setenv("HOME", str(host / "home"))
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setattr(model, "BACKUP_ROOT", host / "backups")
    monkeypatch.setattr(resources, "UNIT_DIR", host / "units")
    monkeypatch.setattr(resources, "APT_SOURCES", host / "sources")
    monkeypatch.setattr(resources, "APT_KEYRINGS", host / "keyrings")
    monkeypatch.setattr(resources, "POLICY_RC", host / "policy-rc.d")
    stamp = host / "apt-stamp"
    stamp.touch()
    monkeypatch.setattr(resources, "APT_STAMPS", (stamp,))
    return host


@pytest.fixture
def sh(sandbox):  # noqa: ARG001
    return FakeShell(RunLog())


@pytest.fixture
def ctx(sh, sandbox):
    return model.Ctx(sh, "demo", cache_dir=sandbox / "cache", stamp="STAMP")


@pytest.fixture
def me():
    """Owner and group that files created by the tests get."""
    user = pwd.getpwuid(os.getuid()).pw_name
    return {"owner": user, "group": grp.getgrgid(os.getegid()).gr_name}


class FakeLibvirt:
    """libvirt as far as the engine talks to it, answering `virsh` in a FakeShell.

    Domains are kept as the XML they were defined with; like the real one it
    adds a UUID, refuses a second domain of the same name with another UUID,
    and refuses writes on the read-only connection.
    """

    NET = (
        "<network><name>default</name>"
        "<ip address='192.168.122.1' netmask='255.255.255.0'><dhcp>"
        "<range start='192.168.122.2' end='192.168.122.254'/>{hosts}</dhcp></ip>"
        "</network>"
    )

    def __init__(self, sh):
        self.domains = {}  # name -> XML
        self.states = {}  # name -> "running" | "shut off"
        self.hosts = []  # "<host .../>" of the default network
        self.active, self.autostart = True, True
        self.calls = []  # every virsh command, without the connection flags
        self.agent = {}  # name -> callable(command dict) -> return value
        self.fail = {}  # first words of a call -> (rc, output)
        sh.replies["virsh"] = self

    def __call__(self, cmd, stdin):  # noqa: C901, PLR0911, PLR0912
        import json  # noqa: PLC0415
        import xml.etree.ElementTree as ET  # noqa: PLC0415

        args = [a for a in cmd[1:] if a not in ("-c", "qemu:///system")]
        readonly = args[0] == "-r"
        args = args[1:] if readonly else args
        self.calls.append(args)
        for words, reply in self.fail.items():
            if " ".join(args).startswith(words):
                return reply
        verb, rest = args[0], args[1:]
        name = next((a for a in rest if not a.startswith("-")), "")
        if verb == "dumpxml":
            return (0, self.domains[name]) if name in self.domains else (1, "")
        if verb == "domstate":
            return (0, self.states[name] + "\n") if name in self.domains else (1, "")
        if verb == "list":
            return 0, "".join(f" - {n} {self.states[n]}\n" for n in self.domains)
        if verb == "net-dumpxml":
            return 0, self.NET.format(hosts="".join(self.hosts))
        if verb == "net-info":
            flags = {"Active": self.active, "Autostart": self.autostart}
            lines = [f"{k}:  {'yes' if v else 'no'}\n" for k, v in flags.items()]
            return 0, "Name: default\n" + "".join(lines) + "Bridge: virbr0\n"
        if readonly:
            return 1, "operation forbidden: read only access prevents it"
        if verb == "define":
            root = ET.fromstring(stdin)
            name = root.findtext("name")
            if root.find("uuid") is None:
                if name in self.domains:
                    return 1, f"domain '{name}' already exists with another uuid"
                ET.SubElement(root, "uuid").text = f"uuid-of-{name}"
            self.domains[name] = ET.tostring(root, encoding="unicode")
            self.states.setdefault(name, "shut off")
        elif verb == "undefine":
            del self.domains[name], self.states[name]
        elif verb == "start":
            self.states[name] = "running"
        elif verb in ("shutdown", "destroy"):
            self.states[name] = "shut off"
        elif verb == "net-update":
            host = rest[3]
            if rest[1] == "add":
                self.hosts.append(host)
            else:
                mac = host.split("mac='")[1].split("'")[0]
                self.hosts = [h for h in self.hosts if mac not in h]
        elif verb == "net-start":
            self.active = True
        elif verb == "net-autostart":
            self.autostart = True
        elif verb == "qemu-agent-command":
            if self.states.get(name) != "running" or name not in self.agent:
                return 1, "Guest agent is not responding"
            answer = self.agent[name](json.loads(rest[1]))
            return 0, json.dumps({"return": answer})
        return 0, ""

    def did(self, verb):
        return [call for call in self.calls if call[0] == verb]


@pytest.fixture
def libvirt(sh):
    return FakeLibvirt(sh)


GLOBAL_CONFIG = """\
[user]
name = Test
email = test@example.com
[init]
defaultBranch = main
[protocol "file"]
allow = always
"""


def git(cwd, *args, check=True):
    """Run git in `cwd`, returning the completed process."""
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def out(cwd, *args):
    return git(cwd, *args).stdout.strip()


def commit_file(repo, name, message):
    (repo / name).write_text(message)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return out(repo, "rev-parse", "HEAD")


@pytest.fixture
def git_env(tmp_path, monkeypatch):
    """Isolate git from the host: no system config, a temp global config.

    The fixture remotes are local paths, so the file protocol is allowed
    here and nowhere else.
    """
    config = tmp_path / "gitconfig"
    config.write_text(GLOBAL_CONFIG)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
    ):
        monkeypatch.delenv(key, raising=False)
    return config


@pytest.fixture
def repos(tmp_path, git_env):  # noqa: ARG001
    """Build remotes, a main checkout, a poisoned config and an unpushed pin.

    super -> sub (absolute URL) -> nested (relative URL `../nested.git`).
    """
    remotes = tmp_path / "remotes"
    urls = {}
    for name in ("super", "sub", "nested"):
        urls[name] = remotes / f"{name}.git"
        git(tmp_path, "init", "--bare", str(urls[name]))

    seed = tmp_path / "seed"
    for name in ("nested", "sub", "super"):
        work = seed / name
        git(tmp_path, "clone", str(urls[name]), str(work))
        if name == "sub":
            git(work, "submodule", "add", "../nested.git", "nested")
        if name == "super":
            git(work, "submodule", "add", str(urls["sub"]), "sub")
        commit_file(work, f"{name}.txt", name)
        git(work, "push", "origin", "main")

    main = tmp_path / "main"
    git(tmp_path, "clone", "--recurse-submodules", str(urls["super"]), str(main))

    # Pin a submodule commit that exists only in the main checkout.
    pin = commit_file(main / "sub", "unpushed.txt", "unpushed")
    git(main, "add", "sub")
    git(main, "commit", "-m", "pin unpushed")

    # A local-path URL in the shared config, pointing nowhere useful.
    poison = tmp_path / "gone" / "sub"
    git(main, "config", "submodule.sub.url", str(poison))

    return SimpleNamespace(
        tmp=tmp_path,
        main=main,
        urls=urls,
        pin=pin,
        poison=poison,
    )
