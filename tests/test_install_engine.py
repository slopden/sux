"""Shell, run log, facts and the single-file build."""

import ast
import json
import os
import subprocess
import sys

import pytest

import build_single_file
from sux.install import facts as facts_mod
from sux.install.facts import Facts
from sux.install.log import Fail, RunLog
from sux.install.recipe import read_tree
from sux.install.shell import Shell

from .conftest import DEMO, FakeShell

pytestmark = pytest.mark.usefixtures("sandbox")


def events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


class TestRunLog:
    def test_events_are_flushed_as_they_happen(self, tmp_path):
        log = RunLog(tmp_path / "deep" / "run.jsonl")
        log.event("start", recipe="demo")
        (first,) = events(log.path)  # readable while the log is still open
        assert first["event"] == "start"
        assert first["recipe"] == "demo"
        assert first["ts"].endswith("+00:00")
        assert first["t"] >= 0
        log.event("end", status="ok")
        log.close()
        assert [e["event"] for e in events(log.path)] == ["start", "end"]

    def test_secrets_are_scrubbed_everywhere(self, tmp_path):
        log = RunLog(tmp_path / "run.jsonl")
        log.secret("hunter2-hunter2")
        log.secret("short")  # too short to be a secret worth masking
        log.secret(None)
        log.event(
            "cmd",
            argv=["tool", "--key=hunter2-hunter2"],
            nested={"env": {"KEY": "x hunter2-hunter2 y"}, "n": 3},
            stdout="short and hunter2-hunter2",
        )
        raw = log.path.read_text()
        assert "hunter2" not in raw
        (event,) = events(log.path)
        assert event["argv"] == ["tool", "--key=<redacted>"]
        assert event["nested"] == {"env": {"KEY": "x <redacted> y"}, "n": 3}
        assert event["stdout"] == "short and <redacted>"

    def test_long_output_is_clipped(self):
        clipped = RunLog.clip("x" * 20_000)
        assert len(clipped) < 17_000
        assert clipped.endswith("<truncated 3616 chars>")
        assert RunLog.clip(None) is None

    def test_path_honours_xdg_state_home(self, sandbox, monkeypatch):
        log = RunLog.for_recipe("demo")
        assert log.path.parent == sandbox / "state" / "sux" / "install"
        assert log.path.name.startswith("demo.")
        assert log.path.suffix == ".jsonl"
        log.close()
        monkeypatch.delenv("XDG_STATE_HOME")
        log = RunLog.for_recipe("demo")
        assert log.path.parent == sandbox / "home/.local/state/sux/install"
        log.close()

    def test_no_path_writes_nothing(self):
        log = RunLog()
        log.event("start")
        log.close()


class TestShell:
    def test_secret_stdin_never_reaches_the_log(self, tmp_path, capsys):
        sh = FakeShell(RunLog(tmp_path / "run.jsonl"))
        sh.run(["tee", "/etc/key"], stdin="s3cr3t-value", secret_stdin=True)
        sh.run(["tee", "/etc/motd"], stdin="hello")
        secret, plain = events(sh.log.path)
        assert secret["stdin"] == "<redacted 12 chars>"
        assert secret["argv"] == ["sudo", "tee", "/etc/key"]
        assert plain["stdin"] == "hello"
        assert "s3cr3t" not in sh.log.path.read_text()
        assert "$ sudo tee /etc/key" in capsys.readouterr().out

    def test_secret_probe_output_is_not_logged(self, tmp_path):
        sh = FakeShell(RunLog(tmp_path / "run.jsonl"))
        sh.sudo_probes = True
        sh.replies["cat"] = (0, "KEY=s3cr3t-value\n")
        assert sh.probe(["cat", "/etc/key"], sudo=True, secret_output=True).stdout
        (event,) = events(sh.log.path)
        assert event["kind"] == "probe"
        assert event["stdout"] == "<redacted>"

    def test_registered_secrets_are_scrubbed_from_output(self, tmp_path):
        sh = FakeShell(RunLog(tmp_path / "run.jsonl"))
        sh.log.secret("s3cr3t-value")
        sh.replies["tool"] = (0, "token is s3cr3t-value\n")
        sh.probe(["tool"])
        assert events(sh.log.path)[0]["stdout"] == "token is <redacted>\n"

    def test_root_probes_wait_for_sudo(self, sh):
        assert sh.probe(["cat", "/etc/shadow"], sudo=True) is None
        assert sh.spawned == []
        sh.elevate()
        assert sh.spawned == [["sudo", "-v"]]
        assert sh.probe(["cat", "/dev/null"], sudo=True).returncode == 0
        assert sh.spawned[-1] == ["sudo", "cat", "/dev/null"]

    def test_refused_sudo(self, sh):
        sh.sudo_ok = False
        with pytest.raises(Fail, match="sudo was refused"):
            sh.elevate()
        assert sh.sudo_probes is False

    def test_failed_command_raises_and_shows_output(self, sh, capsys):
        sh.replies["false"] = (2, "it broke")
        with pytest.raises(Fail, match=r"command failed \(rc=2\): sudo false"):
            sh.run(["false"])
        assert "| it broke" in capsys.readouterr().out
        assert sh.run(["false"], check=False) == 2

    def test_unprivileged_and_root(self, sh):
        sh.run(["true"], sudo=False)
        assert sh.spawned == [["true"]]
        sh.is_root = True
        assert sh.argv(["true"], sudo=True) == ["true"]

    def test_real_processes_get_the_c_locale_and_no_terminal(self, monkeypatch):
        monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
        sh = Shell()
        script = 'printf "%s|" "$LC_ALL"; cat'  # cat would hang on a terminal
        assert sh.out(["sh", "-c", script]) == "C|"
        assert sh.spawn(["cat"], stdin="fed").stdout == "fed"

    def test_probe_env_reaches_the_process_but_not_the_log(self, tmp_path):
        sh = Shell(RunLog(tmp_path / "run.jsonl"))
        r = sh.probe(["sh", "-c", 'printf "%s" "$PASSWD"'], env={"PASSWD": "hunter2"})
        assert r.stdout == "hunter2"
        (event,) = events(sh.log.path)
        assert event["argv"] == ["sh", "-c", 'printf "%s" "$PASSWD"']
        assert "env" not in event

    def test_missing_tool_is_a_failed_probe(self):
        r = Shell().probe(["sux-no-such-tool-xyz"])
        assert r.returncode == 127
        assert r.stdout == ""


class TestFacts:
    def test_values_are_cached(self, sh):
        sh.replies["dpkg --print-architecture"] = (0, "amd64\n")
        facts = Facts(sh)
        assert facts.get("arch") == "amd64"
        assert facts.get("arch") == "amd64"
        assert len(sh.spawned) == 1

    def test_unknown_fact(self, sh):
        with pytest.raises(KeyError):
            Facts(sh).get("shoe_size")

    def test_user(self, sh, monkeypatch, me):
        facts = Facts(sh)
        assert facts.get("invoking_user") == me["owner"]
        assert os.path.isabs(facts.get("home"))
        assert facts.get("invoking_group")
        monkeypatch.setenv("SUDO_USER", "root")
        assert Facts(sh).get("invoking_user") == "root"

    def test_system(self, sh, monkeypatch):
        sh.replies["systemctl --version"] = (0, "systemd 257 (257.4-1ubuntu3)\n+PAM\n")
        monkeypatch.setattr(
            facts_mod.platform,
            "freedesktop_os_release",
            lambda: {"VERSION_CODENAME": "plucky"},
        )
        facts = Facts(sh)
        assert facts.get("systemd") == 257
        assert facts.get("codename") == "plucky"
        assert facts.get("hostname")

    def test_tailscale(self, sh):
        sh.replies["tailscale ip"] = (0, "100.1.2.3\nfd7a::1\n")
        status = {"Self": {"DNSName": "box.tail-1234.ts.net."}}
        sh.replies["tailscale status"] = (0, json.dumps(status))
        facts = Facts(sh)
        assert facts.get("tailscale_ips") == ["100.1.2.3", "fd7a::1"]
        assert facts.get("magicdns") == "box.tail-1234.ts.net"

    def test_no_tailscale(self, sh):
        sh.replies["tailscale"] = (127, "")
        facts = Facts(sh)
        assert facts.get("tailscale_ips") == []
        assert facts.get("magicdns") == ""

    def test_gpu_and_iommu(self, sh, tmp_path, monkeypatch):
        monkeypatch.setattr(facts_mod, "SYSFS", tmp_path)
        assert Facts(sh).get("gpus") == []
        assert Facts(sh).get("iommu") is False
        devices = tmp_path / "bus" / "pci" / "devices"
        for address, cls, vendor in [
            ("0000:00:02.0", "0x030000", "0x8086"),
            ("0000:01:00.0", "0x030000", "0x10de"),
            ("0000:01:00.1", "0x040300", "0x10de"),  # the card's audio function
        ]:
            dev = devices / address
            dev.mkdir(parents=True)
            (dev / "class").write_text(cls + "\n")
            (dev / "vendor").write_text(vendor + "\n")
        (tmp_path / "kernel" / "iommu_groups" / "12").mkdir(parents=True)
        os.symlink(tmp_path / "kernel/iommu_groups/12", devices / "0000:01:00.0/iommu_group")
        os.symlink("/sys/bus/pci/drivers/nvidia", devices / "0000:01:00.0/driver")
        assert Facts(sh).get("iommu") is True
        assert Facts(sh).get("gpus") == [
            {"address": "0000:00:02.0", "vendor": "intel", "driver": "", "iommu_group": ""},
            {
                "address": "0000:01:00.0",
                "vendor": "nvidia",
                "driver": "nvidia",
                "iommu_group": "12",
            },
        ]


class TestSingleFileBuild:
    @pytest.fixture
    def built(self, tmp_path, capsys):
        out = tmp_path / "build" / "sux"
        build_single_file.main(out_path=out, recipes_dir=DEMO.parent)
        capsys.readouterr()
        return out

    def run(self, built, sandbox, *args):
        env = os.environ | {
            "HOME": str(sandbox / "home"),
            "XDG_CONFIG_HOME": str(sandbox / "config"),
            "XDG_STATE_HOME": str(sandbox / "state"),
        }
        return subprocess.run(
            [sys.executable, str(built), *args],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_recipe_tree_is_inlined_as_strings(self, built):
        source = built.read_text()
        tree = ast.parse(source)
        inlined = [
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and getattr(node.targets[0], "id", "") == "BUILTIN_RECIPES"
        ]
        assert len(inlined) == 1
        assert ast.literal_eval(inlined[0]) == {"demo": read_tree(DEMO)}
        assert "from sux" not in source

    def test_empty_recipe_dir_builds(self, tmp_path, capsys):
        out = tmp_path / "sux"
        build_single_file.main(out_path=out, recipes_dir=tmp_path / "none")
        capsys.readouterr()
        assert "BUILTIN_RECIPES = {}" in out.read_text()

    def test_built_file_lists_the_inlined_recipe(self, built, sandbox):
        r = self.run(built, sandbox, "install")
        assert r.returncode == 0, r.stderr
        assert "demo              not set up" in r.stdout

    def test_built_file_without_a_terminal_only_lists_questions(self, built, sandbox):
        r = self.run(built, sandbox, "install", "demo")
        assert r.returncode == 1, r.stderr
        assert "demo has questions that are not answered yet:" in r.stdout
        assert "Folder to serve" in r.stdout
        (log,) = (sandbox / "state" / "sux" / "install").glob("demo.*.jsonl")
        assert [e["event"] for e in events(log)] == ["start", "unanswered", "end"]
        assert not (sandbox / "config" / "sux").exists()

    def test_help_mentions_install(self, built, sandbox):
        r = self.run(built, sandbox, "--help")
        assert r.returncode == 0
        assert "sux install <recipe>" in r.stdout
        r = self.run(built, sandbox, "install", "--help")
        assert "apply / reprobe with sudo / exit" in r.stdout
