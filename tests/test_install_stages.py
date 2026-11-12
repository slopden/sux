"""Stages, recipes that need other recipes, and hook variables."""

import json

import pytest

from sux.install.flow import Unit, install_recipe, load_answers, settle_units
from sux.install.log import Fail
from sux.install.model import Action, Ctx
from sux.install.recipe import (
    build_resources,
    build_verify,
    dependencies,
    dependents,
    load_recipe,
    resolve,
)
from sux.install.facts import Facts

from .conftest import FIXTURES, FakeShell
from .test_install_flow import Gated, scripted

pytestmark = pytest.mark.usefixtures("sandbox")

STAGED = FIXTURES / "staged"
APP, BASE = STAGED / "app", STAGED / "base"


class Host:
    """The fake machine across several runs of the staged recipes."""

    def __init__(self, tmp_path, sandbox):
        self.root = tmp_path / "srv"
        self.root.mkdir()
        self.state = sandbox / "state" / "sux" / "install"
        self.shells = []

    def make_shell(self, log):
        self.shells.append(FakeShell(log))
        return self.shells[-1]

    def run(self, *lines, recipe=APP, tty=True, **kwargs):
        typed = scripted(*lines)
        code = install_recipe(
            str(recipe), input_fn=typed, tty=tty, make_shell=self.make_shell, **kwargs
        )
        return code, self.shells[-1], typed.prompts

    def events(self, name="app"):
        log = sorted(self.state.glob(f"{name}.*.jsonl"))[-1]
        return [json.loads(line) for line in log.read_text().splitlines()]

    def made(self):
        return sorted(p.name for p in self.root.iterdir())


@pytest.fixture
def host(tmp_path, sandbox):
    return Host(tmp_path, sandbox)


def answer_both(host):
    """The two recipes ask for the same folder; base is asked first."""
    return (str(host.root), str(host.root))


# ---------------------------------------------------------------- stages


class TestStages:
    def test_steps_and_verify_entries_are_grouped_by_stage(self, sh):
        recipe = load_recipe(APP)
        assert [s["name"] for s in recipe.stages] == ["one", "two", "three"]
        values, _, _ = resolve(recipe, Facts(sh), {"root": "/srv"})
        two = build_resources(recipe, values, stage="two")
        assert [r.target for r in two] == ["marker two"]
        assert len(build_resources(recipe, values)) == 3  # all of them, for the list
        assert [e["path"] for e in build_verify(recipe, values, "one")] == ["/srv/one"]
        assert build_verify(recipe, values, "three") == []

    def test_a_step_must_name_a_declared_stage(self, tmp_path):
        text = (APP / "recipe.toml").read_text()
        for bad in (text.replace('stage = "two"\nkind', 'stage = "four"\nkind', 1),
                    text.replace('stage = "one"\nkind = "python"\n', 'kind = "python"\n', 1)):  # fmt: skip
            (tmp_path / "recipe.toml").write_text(bad)
            with pytest.raises(Fail, match=r"stage = .* \(stages: one, two, three\)"):
                load_recipe(tmp_path)
        plain = (BASE / "recipe.toml").read_text().replace("[[step]]", '[[step]]\nstage = "x"')
        (tmp_path / "recipe.toml").write_text(plain)
        with pytest.raises(Fail, match="none declared"):
            load_recipe(tmp_path)

    def test_the_plan_shows_the_stages_and_one_prompt_applies_them_in_order(
        self, host, capsys
    ):
        code, sh, prompts = host.run(*answer_both(host), "a", "a")
        assert code == 0
        assert host.made() == ["base", "one", "three", "two-abab"]
        touched = [c.rsplit("/", 1)[1] for c in sh.commands("touch")]
        assert touched == ["base", "one", "two-abab", "three"]
        out = capsys.readouterr().out
        heads = ["base (needed by app)", "Stage 1/3  one: the first thing",
                 "Stage 2/3  two: the second thing", "Stage 3/3  three: the third thing"]  # fmt: skip
        places = [out.index(f"\n{head}\n") for head in heads]
        assert places == sorted(places)
        assert places[-1] < out.index("Applying")  # the whole plan, then the prompt
        # each stage is verified before the next one is applied
        order = ["Applying: Stage 1/3", "ok       one is healthy", "Applying: Stage 2/3",
                 "ok       two-abab is healthy", "Applying: Stage 3/3"]  # fmt: skip
        places = [out.index(text) for text in order]
        assert places == sorted(places)
        # one prompt for the plan; `confirm` asks again before stage three
        assert prompts[2:] == ["apply / exit: ", "apply / exit: "]
        units = [e for e in host.events() if e["event"] == "unit"]
        assert [u["ok"] for u in units] == [True] * 4

    def test_a_failed_stage_stops_the_run_and_keeps_the_earlier_ones(
        self, host, capsys
    ):
        (host.root / "two-abab.broken").touch()
        code, _, _ = host.run(*answer_both(host), "a")
        assert code == 1
        assert host.made() == ["base", "one", "two-abab", "two-abab.broken"]
        out = capsys.readouterr().out
        assert "Stopped at Stage 2/3  two: the second thing" in out
        assert "a re-run resumes here" in out
        assert "FAILED  1 of 1 checks failed: two-abab is broken" in out
        events = host.events()
        assert events[-1]["status"] == "failed"
        failed = [e for e in events if e["event"] == "unit" and not e["ok"]]
        assert failed[0]["title"].startswith("Stage 2/3")
        assert [e["reason"] for e in events if e["event"] == "status"][-1] == "failed"

    def test_a_rerun_resumes_at_the_first_stage_that_is_not_converged(
        self, host, capsys
    ):
        (host.root / "two-abab.broken").touch()
        host.run(*answer_both(host), "a")
        (host.root / "two-abab.broken").unlink()
        capsys.readouterr()
        code, sh, prompts = host.run("a")  # the first pending stage: no second prompt
        assert code == 0
        assert [c.rsplit("/", 1)[1] for c in sh.commands("touch")] == ["three"]
        assert len(prompts) == 1
        out = capsys.readouterr().out
        assert "ok       base (needed by app): nothing to change" in out
        assert "ok       one is healthy" in out  # earlier stages are checked on the way
        assert "ok       two-abab is healthy" in out
        assert out.index("two-abab is healthy") < out.index("Applying: Stage 3/3")

    def test_declining_a_confirm_stops_before_that_stage(self, host, capsys):
        code, _, prompts = host.run(*answer_both(host), "a", "e")
        assert code == 0
        assert host.made() == ["base", "one", "two-abab"]
        assert "Stopped here" in capsys.readouterr().out
        assert len(prompts) == 4

    def test_not_a_terminal_prints_every_stage_and_changes_nothing(
        self, host, capsys
    ):
        host.run(*answer_both(host), "e")
        capsys.readouterr()
        code, sh, _ = host.run(tty=False)
        assert code == 0
        assert host.made() == []
        out = capsys.readouterr().out
        assert out.count("Plan (1 commands)") == 4
        assert "Nothing was changed: stdin is not a terminal." in out

    def test_a_plan_that_changed_after_an_earlier_unit_is_asked_about_again(
        self, sh, capsys
    ):
        first, second = Gated(False, needs_sudo=False), Gated(False, needs_sudo=False)

        def grow():  # applying the first changes what the second has to do
            first.good = True
            second.target = "other thing"

        first.plan = lambda ctx, p: [Action("fix", fn=grow)]
        second.plan = lambda ctx, p: [
            Action(f"fix {second.target}", fn=lambda: setattr(second, "good", True))
        ]
        units = [
            Unit([first], lambda: Ctx(sh, "x"), title="A"),
            Unit([second], lambda: Ctx(sh, "x"), title="B"),
        ]
        typed = scripted("a", "a")
        assert settle_units(units, sh, typed) is True
        assert len(typed.prompts) == 2
        assert "This part of the plan changed" in capsys.readouterr().out


# ---------------------------------------------------------------- needs


class TestNeeds:
    def test_dependencies_in_order_and_circles_refused(self):
        assert [r.name for r in dependencies(load_recipe(APP))] == ["base"]
        assert dependencies(load_recipe(BASE)) == []
        with pytest.raises(Fail, match="circle: loop-a -> loop-b -> loop-a"):
            dependencies(load_recipe(STAGED / "loop-a"))
        assert [r.name for r in dependents(load_recipe(BASE))] == ["app"]

    def test_a_converged_dependency_is_one_line(self, host, capsys):
        host.run(str(host.root), "a", recipe=BASE)
        capsys.readouterr()
        code, sh, _ = host.run(str(host.root), "a", "a")
        assert code == 0
        out = capsys.readouterr().out
        assert "  ok       base (needed by app): nothing to change\n" in out
        assert "base marker" not in out  # no table, no plan
        assert "base is healthy" not in out  # and no health checks of its own
        assert not any("base" in c for c in sh.commands("touch"))

    def test_an_unmet_dependency_is_applied_first_under_the_same_prompt(
        self, host, capsys
    ):
        code, sh, prompts = host.run(*answer_both(host), "a", "a")
        assert code == 0
        assert sh.commands("touch")[0].endswith("/base")
        out = capsys.readouterr().out
        assert out.index("base (needed by app)") < out.index("Stage 1/3")
        assert out.index("ok       base is healthy") < out.index("Applying: Stage 1/3")
        assert load_answers("base") == {"root": str(host.root)}
        assert sh.spawned.count(["sudo", "-v"]) == 1

    def test_remove_leaves_the_dependency_alone(self, host):
        host.run(*answer_both(host), "a", "a")
        code, sh, _ = host.run("a", remove=True)
        assert code == 0
        assert host.made() == ["base"]

    def test_removing_what_an_installed_recipe_needs_is_refused(self, host, capsys):
        host.run(*answer_both(host), "a", "a")
        capsys.readouterr()
        code, sh, prompts = host.run(recipe=BASE, remove=True)
        assert code == 1
        assert prompts == []
        assert host.made() == ["base", "one", "three", "two-abab"]
        out = capsys.readouterr().out
        assert "base is needed by app, which is installed" in out
        assert "sux install app --remove" in out
        host.run("a", remove=True)  # once app is gone, base can go
        code, _, _ = host.run("a", recipe=BASE, remove=True)
        assert code == 0
        assert host.made() == []


class TestHookVariables:
    def test_a_variable_can_come_from_hooks_py(self, sh):
        recipe = load_recipe(APP)
        values, answers, _ = resolve(recipe, Facts(sh), {"root": "/srv"})
        assert values["doubled"] == "abab"
        assert "doubled" not in answers

    def test_a_missing_function_is_named(self, sh, tmp_path):
        text = (APP / "recipe.toml").read_text().replace('"twice"', '"thrice"')
        (tmp_path / "recipe.toml").write_text(text)
        with pytest.raises(Fail, match="var doubled: hooks.py has no function 'thrice'"):
            resolve(load_recipe(tmp_path), Facts(sh), {"root": "/srv"})
