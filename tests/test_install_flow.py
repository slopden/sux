import json
import tomllib

import pytest

from sux import cli
from sux.install import flow
from sux.install import recipe as recipe_mod
from sux.install.facts import FACTS
from sux.install.flow import (
    answers_path,
    ask,
    choose,
    dump_toml,
    install_main,
    install_recipe,
    list_recipes,
    load_answers,
    settle,
)
from sux.install.log import Fail, RunLog
from sux.install.model import UNKNOWN, Action, Ctx, Probe, Resource
from sux.install.recipe import Question

from .conftest import DEMO, FakeShell

pytestmark = pytest.mark.usefixtures("sandbox")


def scripted(*lines):
    """An input() that answers from `lines` and fails on any further prompt."""
    answers = iter(lines)
    prompts = []

    def input_fn(prompt):
        prompts.append(prompt)
        try:
            return next(answers)
        except StopIteration:
            raise AssertionError(f"unexpected prompt: {prompt!r}") from None

    input_fn.prompts = prompts
    return input_fn


# ---------------------------------------------------------------- questions


class TestAsk:
    def q(self, kind, default=None, choices=()):
        return Question("x", "Question", kind, list(choices), default)

    def test_text_and_default(self):
        assert ask(self.q("text"), scripted("  hello ")) == "hello"
        assert ask(self.q("text", "dflt"), scripted("")) == "dflt"
        typed = scripted("")
        ask(self.q("text", "dflt"), typed)
        assert typed.prompts == ["Question [dflt]: "]

    def test_an_answer_is_needed_without_a_default(self, capsys):
        assert ask(self.q("text"), scripted("", "", "x")) == "x"
        assert capsys.readouterr().out.count("an answer is needed") == 2

    def test_path_must_be_absolute(self, capsys, sandbox):
        assert ask(self.q("path"), scripted("relative", "/opt/shared")) == "/opt/shared"
        assert "give an absolute path" in capsys.readouterr().out
        assert ask(self.q("path"), scripted("~/x")) == str(sandbox / "home" / "x")

    def test_bool(self, capsys):
        assert ask(self.q("bool", False), scripted("y")) is True
        assert ask(self.q("bool", True), scripted("No")) is False
        assert ask(self.q("bool", True), scripted("")) is True
        assert ask(self.q("bool"), scripted("maybe", "n")) is False
        assert "answer y or n" in capsys.readouterr().out
        typed = scripted("")
        ask(self.q("bool", False), typed)
        assert typed.prompts == ["Question [y/N]: "]

    def test_choice(self, capsys):
        q = self.q("choice", "info", ["info", "debug"])
        assert ask(q, scripted("")) == "info"
        assert ask(q, scripted("2")) == "debug"
        assert ask(q, scripted("debug")) == "debug"
        assert ask(q, scripted("3", "trace", "1")) == "info"
        out = capsys.readouterr().out
        assert "  1. info\n  2. debug\n" in out
        assert "'trace' is not one of the choices" in out

    def test_multi(self, capsys):
        q = self.q("multi", ["smb"], ["smb", "s3"])
        assert ask(q, scripted("")) == ["smb"]
        assert ask(q, scripted("2, 1")) == ["smb", "s3"]  # in the declared order
        assert ask(q, scripted("s3")) == ["s3"]
        assert ask(q, scripted("none")) == []
        assert ask(q, scripted("smb,nfs", "1")) == ["smb"]
        out = capsys.readouterr().out
        assert "  1. [x] smb\n  2. [ ] s3\n" in out

    def test_answers_round_trip_through_toml(self):
        answers = {
            "root": '/opt/"quoted"\\x',
            "protocols": ["smb", "s3"],
            "none": [],
            "tls": False,
            "port": 7070,
        }
        assert tomllib.loads(dump_toml(answers)) == answers

    def test_answers_path_honours_xdg(self, sandbox, monkeypatch):
        assert answers_path("demo") == sandbox / "config/sux/install/demo.toml"
        monkeypatch.delenv("XDG_CONFIG_HOME")
        assert answers_path("demo") == sandbox / "home/.config/sux/install/demo.toml"


# ---------------------------------------------------------------- the prompt


class TestChoose:
    def test_options_depend_on_state(self):
        typed = scripted("a")
        assert choose(True, True, typed) == "apply"
        assert typed.prompts == ["\n[a]pply / [r]eprobe with sudo / [e]xit: "]
        typed = scripted("r")
        assert choose(False, True, typed) == "reprobe"
        assert typed.prompts == ["\n[r]eprobe with sudo / [e]xit: "]
        typed = scripted("e")
        assert choose(True, False, typed) == "exit"
        assert typed.prompts == ["\n[a]pply / [e]xit: "]

    def test_only_offered_options_are_accepted(self):
        assert choose(True, False, scripted("r", "reprobe", "", "yes", "apply")) == (
            "apply"
        )
        assert choose(False, True, scripted("a", "Exit")) == "exit"

    def test_end_of_input_is_exit(self):
        def closed(_prompt):
            raise EOFError

        assert choose(True, True, closed) == "exit"


class Gated(Resource):
    """Unknown without sudo; with sudo, needs one change unless `good`."""

    kind = "gated"

    def __init__(self, good, needs_sudo=True, fixable=True):
        super().__init__({})
        self.target = "thing"
        self.good, self.needs_sudo, self.fixable = good, needs_sudo, fixable

    def probe(self, ctx):
        if self.needs_sudo and not ctx.sh.sudo_probes:
            return Probe(None, UNKNOWN)
        return Probe(self.good, "fine" if self.good else "wrong")

    def plan(self, ctx, p):  # noqa: ARG002
        def fix():
            self.good = self.fixable

        return [Action("fix the thing", fn=fix)]


class TestSettle:
    def run(self, sh, res, *lines):
        typed = scripted(*lines)
        done = settle(res, sh, lambda: Ctx(sh, "demo"), typed)
        return done, typed.prompts

    def test_nothing_to_change_skips_the_prompt(self, sh, capsys):
        done, prompts = self.run(sh, [Gated(True, needs_sudo=False)])
        assert done is True
        assert prompts == []
        assert sh.spawned == []
        assert "Plan: nothing to change" in capsys.readouterr().out

    def test_not_a_terminal_changes_nothing(self, sh, capsys):
        res = Gated(False, needs_sudo=False)
        assert settle([res], sh, lambda: Ctx(sh, "demo"), None) is False
        assert res.good is False
        assert sh.spawned == []
        out = capsys.readouterr().out
        assert "Plan (1 commands)" in out
        assert "fix the thing" in out
        assert "Nothing was changed: stdin is not a terminal." in out

    def test_exit_changes_nothing(self, sh):
        res = Gated(False, needs_sudo=False)
        done, prompts = self.run(sh, [res], "e")
        assert done is False
        assert res.good is False
        assert prompts == ["\n[a]pply / [e]xit: "]  # no unknowns: no reprobe offered
        assert sh.spawned == []  # sudo is not touched before the choice

    def test_apply_then_converge(self, sh, capsys):
        res = Gated(False, needs_sudo=False)
        done, _ = self.run(sh, [res], "a")
        assert done is True
        assert res.good is True
        assert sh.spawned == [["sudo", "-v"]]
        assert "re-probed: all 1 items match" in capsys.readouterr().out

    def test_unknown_offers_only_reprobe_until_resolved(self, sh, capsys):
        done, prompts = self.run(sh, [Gated(True)], "r")
        assert done is True
        assert prompts == ["\n[r]eprobe with sudo / [e]xit: "]
        assert sh.spawned == [["sudo", "-v"]]
        out = capsys.readouterr().out
        assert "unknown until sudo" in out
        assert "1 item(s) cannot be checked without sudo" in out

    def test_reprobe_reveals_a_change_then_reprompts(self, sh):
        res = Gated(False)
        done, prompts = self.run(sh, [res], "r", "a")
        assert done is True
        assert res.good is True
        assert prompts == [
            "\n[r]eprobe with sudo / [e]xit: ",
            "\n[a]pply / [e]xit: ",  # resolved: reprobe is no longer offered
        ]

    def test_apply_with_unknowns_reprompts_if_the_plan_grew(self, sh, capsys):
        visible, hidden = Gated(False, needs_sudo=False), Gated(False)
        done, prompts = self.run(sh, [visible, hidden], "a", "e")
        assert done is False
        assert (visible.good, hidden.good) == (False, False)  # nothing ran
        assert prompts == [
            "\n[a]pply / [r]eprobe with sudo / [e]xit: ",
            "\n[a]pply / [e]xit: ",
        ]
        assert "The plan changed once sudo could check" in capsys.readouterr().out

    def test_apply_with_unknowns_proceeds_if_the_plan_held(self, sh):
        visible, hidden = Gated(False, needs_sudo=False), Gated(True)
        done, prompts = self.run(sh, [visible, hidden], "a")
        assert done is True
        assert visible.good is True
        assert len(prompts) == 1

    def test_refused_sudo_stops_before_any_change(self, sh):
        sh.sudo_ok = False
        res = Gated(False, needs_sudo=False)
        with pytest.raises(Fail, match="sudo was refused"):
            self.run(sh, [res], "a")
        assert res.good is False

    def test_not_converged_after_apply_fails(self, sh):
        with pytest.raises(Fail, match="not converged: gated:thing"):
            self.run(sh, [Gated(False, needs_sudo=False, fixable=False)], "a")


# ---------------------------------------------------------------- end to end


class Host:
    """The fake machine across several runs of the demo recipe."""

    def __init__(self, tmp_path, sandbox):
        self.root = tmp_path / "srv"
        self.unit_dir = sandbox / "units"
        self.state = sandbox / "state" / "sux" / "install"
        self.units = {}
        self.shells = []
        self.first_answers = (str(self.root), str(self.unit_dir), "", "", "", "")

    def make_shell(self, log):
        sh = FakeShell(log)
        sh.units = self.units
        self.shells.append(sh)
        return sh

    def run(self, *lines, tty=True, **kwargs):
        typed = scripted(*lines)
        code = install_recipe(
            str(DEMO), input_fn=typed, tty=tty, make_shell=self.make_shell, **kwargs
        )
        return code, self.shells[-1], typed.prompts

    def events(self):
        log = sorted(self.state.glob("demo.*.jsonl"))[-1]
        return [json.loads(line) for line in log.read_text().splitlines()]

    def last_run(self):
        events = self.events()
        start = max(i for i, e in enumerate(events) if e["event"] == "start")
        return events[start:]


@pytest.fixture
def host(tmp_path, sandbox, monkeypatch, me):
    monkeypatch.setitem(FACTS, "invoking_group", lambda _sh: me["group"])
    monkeypatch.setitem(FACTS, "arch", lambda _sh: "amd64")
    return Host(tmp_path, sandbox)


class TestInstall:
    def test_first_run_asks_saves_applies_and_verifies(self, host, capsys, me):
        code, sh, prompts = host.run(*host.first_answers, "a")
        assert code == 0
        assert len(prompts) == 7  # six questions, then the one prompt
        assert prompts[0] == "Folder to serve: "
        assert prompts[2] == f"Run as user [{me['owner']}]: "
        assert prompts[-1] == "\n[a]pply / [e]xit: "
        assert load_answers("demo") == {
            "root": str(host.root),
            "unit_dir": str(host.unit_dir),
            "user": me["owner"],
            "features": ["web"],
            "level": "info",
            "tls": False,
        }

        conf = host.root / "etc" / "demo.conf"
        assert "listen = 127.0.0.1:8080\n" in conf.read_text()
        assert (host.unit_dir / "demo-web.service").exists()
        assert (host.root / "marker").exists()
        assert host.units["demo-web.service"]["active"]
        changes = sh.changes()
        assert changes.index("systemctl daemon-reload") < changes.index(
            "systemctl restart demo-web.service"
        )

        out = capsys.readouterr().out
        assert "CHANGE" in out
        assert f"+root = {host.root}" in out  # the diff is part of the plan
        assert "re-probed: all 6 items match the desired state" in out
        assert "ok  active: demo-web.service" in out
        assert f"ok  {conf} names its root" in out
        assert out.index("Plan (") < out.index("Applying") < out.index("Health checks")

        events = host.last_run()
        assert events[0]["event"] == "start"
        assert len(events[0]["recipe_sha256"]) == 64
        assert events[-1]["event"] == "end"
        assert events[-1]["status"] == "ok"
        kinds = {e["event"] for e in events}
        assert {
            "answers",
            "resource",
            "plan_action",
            "choice",
            "cmd",
            "converged",
        } <= kinds

    def test_second_run_asks_nothing_and_skips_the_prompt(self, host, capsys):
        host.run(*host.first_answers, "a")
        capsys.readouterr()
        code, sh, prompts = host.run()  # any prompt at all would fail the test
        assert code == 0
        assert prompts == []
        assert sh.changes() == []
        assert ["sudo", "-v"] not in sh.spawned
        out = capsys.readouterr().out
        assert "Plan: nothing to change" in out
        assert "Health checks" in out

    def test_reconfigure_reasks_and_unticking_plans_removal(self, host, capsys):
        host.run(*host.first_answers, "a")
        capsys.readouterr()
        code, sh, prompts = host.run(
            "", "", "", "metrics", "debug", "", "a", reconfigure=True
        )
        assert code == 0
        assert prompts[0] == f"Folder to serve [{host.root}]: "  # saved answer
        assert load_answers("demo")["features"] == ["metrics"]
        assert load_answers("demo")["level"] == "debug"

        unit_file = host.unit_dir / "demo-web.service"
        changes = sh.changes()
        stop = changes.index("systemctl disable --now demo-web.service")
        assert stop < changes.index(f"rm -f {unit_file}")
        assert any(c.startswith(f"cp -a --parents {unit_file} ") for c in changes)
        assert "systemctl daemon-reload" in changes
        assert not unit_file.exists()
        assert not host.units["demo-web.service"]["active"]
        conf = (host.root / "etc" / "demo.conf").read_text()
        assert "[metrics]\nenabled = true\n" in conf
        assert "level = debug\n" in conf

        out = capsys.readouterr().out
        assert "(absent)" in out
        assert "+[metrics]" in out
        assert "active: demo-web.service" not in out  # its verify is gated too
        assert "names its root" in out

    def test_not_a_terminal_lists_unanswered_questions(self, host, capsys):
        code, sh, prompts = host.run(tty=False)
        assert code == 1
        assert prompts == []
        assert sh.spawned == []
        assert not answers_path("demo").exists()
        out = capsys.readouterr().out
        assert "demo has questions that are not answered yet:" in out
        assert "root            Folder to serve" in out
        assert "features        Features" in out
        events = host.last_run()
        assert events[-2]["event"] == "unanswered"
        assert events[-1] == events[-1] | {"event": "end", "status": "incomplete"}

    def test_not_a_terminal_prints_the_plan_and_changes_nothing(self, host, capsys):
        host.run(*host.first_answers, "e")
        capsys.readouterr()
        code, sh, prompts = host.run(tty=False)
        assert code == 0
        assert prompts == []
        assert sh.changes() == []
        assert ["sudo", "-v"] not in sh.spawned
        assert not host.root.exists()
        out = capsys.readouterr().out
        assert "Plan (" in out
        assert f"+root = {host.root}" in out
        assert "Nothing was changed: stdin is not a terminal." in out
        assert "Health checks" not in out
        assert host.last_run()[-1]["status"] == "ok"

    def test_not_a_terminal_cannot_reconfigure(self, host, capsys):
        host.run(*host.first_answers, "e")
        capsys.readouterr()
        code, _, _ = host.run(tty=False, reconfigure=True)
        assert code == 1
        assert "not answered yet" in capsys.readouterr().out

    def test_exit_leaves_the_machine_alone(self, host, capsys):
        code, sh, _ = host.run(*host.first_answers, "e")
        assert code == 0
        assert sh.changes() == []
        assert ["sudo", "-v"] not in sh.spawned
        assert not host.root.exists()
        assert answers_path("demo").exists()  # the answers are kept
        assert "Nothing was changed." in capsys.readouterr().out

    def test_remove_plans_the_inverse(self, host, capsys):
        host.run(*host.first_answers, "a")
        capsys.readouterr()
        code, sh, _ = host.run("a", remove=True)
        assert code == 0
        assert not (host.unit_dir / "demo-web.service").exists()
        assert not (host.root / "etc" / "demo.conf").exists()
        assert not (host.root / "etc").exists()
        assert not (host.root / "marker").exists()
        assert not host.units["demo-web.service"]["active"]
        assert "Health checks" not in capsys.readouterr().out
        assert host.last_run()[0]["mode"] == "remove"

        code, sh, prompts = host.run(remove=True)  # nothing left: no prompt
        assert code == 0
        assert prompts == []

    def test_drift_is_found_and_repaired(self, host, capsys):
        host.run(*host.first_answers, "a")
        conf = host.root / "etc" / "demo.conf"
        conf.write_text(conf.read_text() + "tampered = yes\n")
        capsys.readouterr()
        code, sh, _ = host.run("a")
        assert code == 0
        assert "tampered" not in conf.read_text()
        out = capsys.readouterr().out
        assert "-tampered = yes" in out
        changes = sh.changes()
        assert any(c.startswith(f"cp -a --parents {conf} ") for c in changes)
        # the unit is restarted because its config changes in this run
        assert "systemctl restart demo-web.service" in changes

    def test_failure_is_reported_and_the_log_still_ends(self, host, capsys):
        host.make_shell_orig = host.make_shell

        def failing(log):
            sh = host.make_shell_orig(log)
            sh.replies["systemctl restart"] = (1, "Job failed")
            return sh

        host.make_shell = failing
        code, _, _ = host.run(*host.first_answers, "a")
        assert code == 1
        out = capsys.readouterr().out
        assert "ERROR: command failed (rc=1): sudo systemctl restart demo-web" in out
        assert "| Job failed" in out
        assert "log: " in out
        end = host.last_run()[-1]
        assert end["event"] == "end"
        assert end["status"] == "failed"
        assert "command failed" in end["error"]

    def test_crash_still_writes_the_end_event(self, host, capsys, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(flow, "build_resources", boom)
        code, _, _ = host.run(*host.first_answers)
        assert code == 1
        assert "CRASHED: RuntimeError('boom')" in capsys.readouterr().out
        end = host.last_run()[-1]
        assert end["status"] == "crashed"
        assert "Traceback" in end["error"]

    def test_requirements_stop_before_the_plan(self, host, capsys, monkeypatch):
        monkeypatch.setitem(FACTS, "arch", lambda _sh: "riscv64")
        code, sh, _ = host.run(*host.first_answers)
        assert code == 1
        assert "requires arch" in capsys.readouterr().out
        assert sh.changes() == []

    def test_unknown_recipe(self, capsys):
        assert install_recipe("nope") == 1
        assert "unknown recipe 'nope'" in capsys.readouterr().out

    def test_listing_shows_each_recipe_state(self, host, capsys, monkeypatch):
        monkeypatch.setattr(recipe_mod, "RECIPES_DIR", DEMO.parent)
        list_recipes(host.make_shell(RunLog()))
        assert "demo                not set up" in capsys.readouterr().out
        host.run(*host.first_answers, "e")
        capsys.readouterr()
        list_recipes(host.make_shell(RunLog()))
        assert "demo                6 to change" in capsys.readouterr().out
        host.run("a")
        capsys.readouterr()
        sh = host.make_shell(RunLog())
        list_recipes(sh)
        out = capsys.readouterr().out
        assert "demo                installed" in out
        assert "Example recipe for the tests" in out
        assert sh.changes() == []

    def test_listing_without_recipes(self, sh, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr(recipe_mod, "RECIPES_DIR", tmp_path)
        assert list_recipes(sh) == 0
        assert capsys.readouterr().out == "no recipes\n"


# ---------------------------------------------------------------- cli


class TestCli:
    def test_install_is_taken_before_the_flat_parser(self, monkeypatch):
        seen = []
        monkeypatch.setattr(cli, "install_main", lambda argv: seen.append(argv) or 3)
        monkeypatch.setattr("sys.argv", ["sux", "install", "demo", "--remove"])
        assert cli.main() == 3
        assert seen == [["demo", "--remove"]]

    def test_arguments(self, monkeypatch):
        seen = []

        def fake(ref, **kwargs):
            seen.append((ref, kwargs))
            return 0

        monkeypatch.setattr(flow, "install_recipe", fake)
        assert install_main(["demo"]) == 0
        assert install_main(["demo", "--remove"]) == 0
        assert install_main(["demo", "--reconfigure"]) == 0
        assert seen == [
            ("demo", {"remove": False, "reconfigure": False}),
            ("demo", {"remove": True, "reconfigure": False}),
            ("demo", {"remove": False, "reconfigure": True}),
        ]

    def test_no_recipe_lists(self, monkeypatch):
        monkeypatch.setattr(flow, "list_recipes", lambda _sh: 7)
        assert install_main([]) == 7

    def test_old_flags_are_gone(self, capsys):
        for flag in ("--apply", "--verify", "--sudo-probe"):
            with pytest.raises(SystemExit):
                install_main(["demo", flag])
        assert "unrecognized arguments" in capsys.readouterr().err


# ---------------------------------------------------------------- status


class TestStatus:
    def status(self, host):
        return [e for e in host.last_run() if e["event"] == "status"]

    def test_snapshot_after_an_apply(self, host):
        host.run(*host.first_answers, "a")
        (event,) = self.status(host)
        assert event["reason"] == "applied"
        assert event["units"] == ["demo-web.service"]
        argvs = [c["argv"] for c in event["commands"]]
        assert argvs == [
            ["systemctl", "status", "--no-pager", "-l", "demo-web.service"],
            ["sudo", "-n", "journalctl", "-u", "demo-web.service", "-n", "80"]
            + ["--no-pager", "-o", "short-iso"],
            ["sudo", "-n", "ss", "-ltnp"],  # sudo is in use after the apply
        ]
        assert event["commands"][0]["output"] == "status of demo-web.service"
        assert event["skipped"] == []
        events = [e["event"] for e in host.last_run()]
        assert events.index("converged") < events.index("status")

    def test_no_snapshot_when_nothing_ran(self, host):
        host.run(*host.first_answers, "e")
        assert self.status(host) == []
        host.run("a")
        host.run()  # nothing to change, health checks pass
        assert self.status(host) == []

    def test_snapshot_on_a_failed_action(self, host):
        make = host.make_shell

        def failing(log):
            sh = make(log)
            sh.replies["systemctl restart"] = (1, "Job failed")
            return sh

        host.make_shell = failing
        code, _, _ = host.run(*host.first_answers, "a")
        assert code == 1
        (event,) = self.status(host)
        assert event["reason"] == "failed"
        assert host.last_run()[-1]["status"] == "failed"

    def test_snapshot_on_a_failed_health_check_without_sudo(self, host):
        host.run(*host.first_answers, "a")
        host.units["demo-web.service"]["active"] = False  # it died since
        host.units["demo-web.service"]["enabled"] = "enabled"
        code, sh, _ = host.run(tty=False)  # plans a restart, changes nothing
        assert self.status(host) == []
        (host.root / "etc" / "demo.conf").write_text("no ro" + "ot here\n")
        host.units["demo-web.service"]["active"] = True
        code, sh, _ = host.run(tty=False)
        assert code == 0  # a pending change is not a failure: no snapshot
        assert self.status(host) == []

    def test_what_needs_sudo_is_skipped_until_sudo_is_in_use(self, sh, tmp_path):
        sh.log = RunLog(tmp_path / "run.jsonl")
        extra = [{"argv": ["tool", "-s"]}, {"argv": ["virsh", "list"], "sudo": True}]
        flow.collect_status(sh, ["a", "a.service"], extra, "failed")
        (event,) = [json.loads(ln) for ln in sh.log.path.read_text().splitlines()]
        ran = [c["argv"][0] for c in event["commands"]]
        assert ran == ["systemctl", "journalctl", "ss", "tool"]  # as this user
        assert event["skipped"] == [["virsh", "list"]]
        assert event["units"] == ["a.service"]  # one unit under two spellings
        assert "sudo" not in [a[0] for a in sh.spawned]

    def test_output_is_clipped_and_scrubbed_and_a_missing_tool_is_noted(
        self, sh, tmp_path
    ):
        sh.log = RunLog(tmp_path / "run.jsonl")
        sh.log.secret("hunter2-hunter2")
        sh.sudo_probes = True
        sh.replies["ss -ltnp"] = (0, "key hunter2-hunter2 " + "x" * 20_000)

        def spawn(argv, **kwargs):
            if argv[0] == "virsh":
                raise FileNotFoundError("virsh")
            return FakeShell.spawn(sh, argv, **kwargs)

        sh.spawn = spawn
        flow.collect_status(sh, [], [{"argv": ["virsh", "list"]}], "failed")
        (event,) = [json.loads(ln) for ln in sh.log.path.read_text().splitlines()]
        listeners, virsh = event["commands"]
        assert listeners["output"].startswith("key <redacted> xxx")
        assert "<truncated" in listeners["output"]
        assert (virsh["rc"], virsh["output"]) == (127, "virsh")

    def test_a_crash_is_snapshotted_too(self, host, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(flow, "settle", boom)
        code, _, _ = host.run(*host.first_answers)
        assert code == 1
        assert [e["reason"] for e in self.status(host)] == ["failed"]
        assert host.last_run()[-1]["status"] == "crashed"
