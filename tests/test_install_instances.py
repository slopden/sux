"""Recipes that manage a list: `[instances]`, `each`, `collect`, add/edit/remove."""

import tomllib

import pytest

from sux.install.facts import FACTS, Facts
from sux.install.flow import dump_toml, edit_instances, install_recipe, load_answers
from sux.install.log import Fail
from sux.install.recipe import build_resources, build_verify, load_recipe, resolve
from sux.install.resources import DirRes

from .conftest import SITES, FakeShell
from .test_install_flow import scripted

pytestmark = pytest.mark.usefixtures("sandbox")
MENU = "\n[a]dd / [e]dit / [r]emove / [d]one: "


@pytest.fixture
def facts(sh, monkeypatch, me):
    monkeypatch.setitem(FACTS, "invoking_user", lambda _sh: me["owner"])
    monkeypatch.setitem(FACTS, "invoking_group", lambda _sh: me["group"])
    return Facts(sh)


@pytest.fixture
def recipe():
    return load_recipe(str(SITES))


def site(tmp_path, name, serve=("web",), **more):
    return {"name": name, "root": str(tmp_path / name), "serve": list(serve)} | more


def saved(tmp_path, *sites):
    return {"base": str(tmp_path / "base"), "site": list(sites)}


def keys(resources, present=True):
    return [r.key for r in resources if r.present is present]


# ---------------------------------------------------------------- variables


class TestResolve:
    def test_each_table_is_an_instance(self, recipe, facts, tmp_path):
        rows = [site(tmp_path, "a"), site(tmp_path, "b", ["web", "ftp"], port=2121)]
        values, answers, unanswered = resolve(recipe, facts, saved(tmp_path, *rows))
        assert unanswered == []
        assert answers["site"] == rows
        first, second = values["site"]
        assert first == rows[0] | {"port": 21, "conf": f"{tmp_path}/a/a"}
        assert second["port"] == 2121
        assert values.gone["site"] == []

    def test_collect_gathers_one_variable_across_instances(
        self, recipe, facts, tmp_path
    ):
        rows = [
            site(tmp_path, "a"),
            site(tmp_path, "b", ["ftp"]),
            site(tmp_path, "c", ["web", "ftp"]) | {"root": str(tmp_path / "a")},
        ]
        values, _, _ = resolve(recipe, facts, saved(tmp_path, *rows))
        assert values["web"] == ["a", "c"]  # only where `when` holds
        assert values["roots"] == [str(tmp_path / "a"), str(tmp_path / "b")]  # unique

    def test_no_instances_yet_means_unanswered_questions(self, recipe, facts, tmp_path):
        _, _, unanswered = resolve(recipe, facts, {"base": str(tmp_path)})
        assert [q.name for q in unanswered] == ["name", "root", "serve"]

    def test_a_question_whose_when_is_false_is_not_asked(self, recipe, facts, tmp_path):
        row = site(tmp_path, "a", ["ftp"])  # ftp: the port is a question now
        _, _, unanswered = resolve(recipe, facts, saved(tmp_path, row))
        assert [(q.name, q.default) for q in unanswered] == [("port", 21)]

    def test_removed_instances_are_kept_apart(self, recipe, facts, tmp_path):
        rows = [site(tmp_path, "a"), site(tmp_path, "b") | {"removed": True}]
        values, answers, _ = resolve(recipe, facts, saved(tmp_path, *rows))
        assert [s["name"] for s in values["site"]] == ["a"]
        assert [s["name"] for s in values.gone["site"]] == ["b"]
        assert values["web"] == ["a"]
        assert answers["site"][1]["removed"] is True

    def test_bad_answers(self, recipe, facts, tmp_path):
        twice = saved(tmp_path, site(tmp_path, "a"), site(tmp_path, "a"))
        with pytest.raises(Fail, match="more than one site has name = a"):
            resolve(recipe, facts, twice)
        with pytest.raises(
            Fail, match=r"site must be a list of tables \(\[\[site\]\]\)"
        ):
            resolve(recipe, facts, {"base": "/x", "site": "a"})

    def test_answers_round_trip_through_toml(self, tmp_path):
        answers = saved(tmp_path, site(tmp_path, "a"), site(tmp_path, "b", [], port=1))
        answers["site"][1]["removed"] = True
        text = dump_toml(answers)
        assert text.count("[[site]]") == 2
        assert tomllib.loads(text) == answers
        assert tomllib.loads(dump_toml({"base": "/x", "site": []}))["site"] == []


# ---------------------------------------------------------------- steps


class TestEach:
    def build(self, recipe, facts, tmp_path, *rows, **kwargs):
        values, _, _ = resolve(recipe, facts, saved(tmp_path, *rows))
        return values, build_resources(recipe, values, **kwargs)

    def test_steps_repeat_per_instance_in_file_order(self, recipe, facts, tmp_path):
        rows = [site(tmp_path, "a"), site(tmp_path, "b", ["web", "ftp"])]
        _, resources = self.build(recipe, facts, tmp_path, *rows)
        a, b = tmp_path / "a", tmp_path / "b"
        assert keys(resources) == [
            f"file:{tmp_path}/base/index.conf",
            f"dir:{a}",
            f"dir:{b}",
            f"file:{a}/a.web",
            f"file:{b}/b.web",
            f"file:{b}/b.ftp",
            "systemd:daemon-reload",
            "unit:web-a.service",
            "unit:web-b.service",
            "python:note 127.0.0.1",  # `each` over a plain list: $item and $host
            "python:note [::1]",
        ]
        assert keys(resources, present=False) == [f"file:{a}/a.ftp"]

    def test_fragments_render_once_from_the_whole_list(self, recipe, facts, tmp_path):
        rows = [site(tmp_path, "a"), site(tmp_path, "b", ["ftp"]), site(tmp_path, "c")]
        _, resources = self.build(recipe, facts, tmp_path, *rows)
        index = next(r for r in resources if r.target.endswith("index.conf"))
        assert index.content == (
            "# Managed by sux install.\n"
            "listen = 127.0.0.1:80  # 127.0.0.1\n"  # inline `text`, per item
            "listen = [::1]:80  # ::1\n"
            f"[a]\nroot = {tmp_path}/a\n"
            f"[c]\nroot = {tmp_path}/c\n"  # b: its `when` is false
        )

    def test_a_removed_instance_plans_removal_of_what_it_owned(
        self, recipe, facts, tmp_path
    ):
        rows = [site(tmp_path, "a"), site(tmp_path, "b") | {"removed": True}]
        _, resources = self.build(recipe, facts, tmp_path, *rows)
        b = tmp_path / "b"
        assert keys(resources, present=False) == [
            "unit:web-b.service",  # removals first, last step first
            f"file:{b}/b.ftp",
            f"file:{tmp_path}/a/a.ftp",  # a's `when`: it does not serve ftp
            f"file:{b}/b.web",
            f"dir:{b}",
        ]
        assert "unit:web-a.service" in keys(resources)
        index = next(r for r in resources if r.target.endswith("index.conf"))
        assert "[a]" in index.content
        assert "[b]" not in index.content

    def test_remove_inverts_every_instance(self, recipe, facts, tmp_path):
        rows = [site(tmp_path, "a"), site(tmp_path, "b")]
        _, resources = self.build(recipe, facts, tmp_path, *rows, remove=True)
        assert keys(resources) == ["systemd:daemon-reload (last)"]
        assert len(keys(resources, present=False)) == 11

    def test_a_cleanup_step_only_ever_removes(self, recipe, facts, tmp_path, ctx):
        left = {"kind": "dir", "each": "site", "when": "'ftp' in serve"}
        recipe.data["step"].append(left | {"cleanup": True, "path": "$root/.locks"})
        rows = [site(tmp_path, "a"), site(tmp_path, "b", ["ftp"])]
        _, resources = self.build(recipe, facts, tmp_path, *rows)
        locks = [r for r in resources if r.target.endswith(".locks")]
        assert [(r.target, r.present) for r in locks] == [
            (f"{tmp_path}/a/.locks", False)  # b serves ftp: nothing is built
        ]
        (res,) = locks
        assert res.probe(ctx).ok is True  # not there
        res.path.mkdir(parents=True)
        assert [a.argv for a in res.plan(ctx, res.probe(ctx))] == [
            ["rmdir", str(res.path)]
        ]
        (res.path / "lock").write_text("held")
        assert res.probe(ctx).have.startswith("not empty, kept")

    def test_each_needs_a_list(self, recipe, facts, tmp_path):
        recipe.data["step"][0]["each"] = "base"
        values, _, _ = resolve(recipe, facts, saved(tmp_path, site(tmp_path, "a")))
        with pytest.raises(Fail, match="step 1 .file.: each = 'base' is not a list"):
            build_resources(recipe, values)

    def test_verify_entries_repeat_too(self, recipe, facts, tmp_path):
        rows = [site(tmp_path, "a"), site(tmp_path, "b", ["ftp"]), site(tmp_path, "c")]
        gone = site(tmp_path, "d") | {"removed": True}
        values, _, _ = resolve(recipe, facts, saved(tmp_path, *rows, gone))
        assert build_verify(recipe, values) == [
            {"kind": "unit_active", "name": "web-a.service"},
            {"kind": "unit_active", "name": "web-c.service"},
        ]


class TestEmptyTree:
    def test_a_tree_without_data_is_removed(self, tmp_path, me, ctx, sh):
        res = DirRes(
            {"path": str(tmp_path / "root"), "remove_empty_tree": True} | me,
            present=False,
        )
        assert res.probe(ctx).ok is True  # absent
        (res.path / "proc").mkdir(parents=True)
        (res.path / "usr" / "bin").mkdir(parents=True)
        (res.path / "usr" / "bin" / "tool").touch()  # a bind mount point
        (res.path / "bin").symlink_to("usr/bin")
        p = res.probe(ctx)
        assert (p.ok, p.have.split(" (")[0]) == (False, "present, holds no data")
        (action,) = res.plan(ctx, p)
        assert action.argv[:4] == ["find", str(res.path), "-xdev", "-depth"]
        assert action.argv[-1] == "-delete"
        action.execute(sh)
        assert not res.path.exists()

    def test_data_is_kept_unless_this_run_removes_it(self, tmp_path, me, ctx):
        res = DirRes(
            {"path": str(tmp_path / "root"), "remove_empty_tree": True} | me,
            present=False,
        )
        res.path.mkdir()
        (res.path / "notes").write_text("mine")
        p = res.probe(ctx)
        assert p.ok is True
        assert p.have == f"holds data, kept ({res.path}/notes)"
        ctx.changing.add(f"file:{res.path}/notes")
        assert res.probe(ctx).ok is False

    def test_unreadable_parts_wait_for_sudo(self, tmp_path, me, ctx, sh):
        res = DirRes(
            {"path": str(tmp_path / "root"), "remove_empty_tree": True} | me,
            present=False,
        )
        res.path.mkdir()
        sh.replies["find"] = (1, "")  # Permission denied below it
        assert res.probe(ctx).ok is None
        sh.sudo_probes = True
        assert res.probe(ctx).ok is False
        assert sh.spawned[-1][:2] == ["sudo", "find"]


# ---------------------------------------------------------------- the menu


class TestMenu:
    SPEC = {"name": "site", "key": "name"}

    def edit(self, rows, *lines, asked=()):
        """Run the menu; `asked` are the answers `one` gives, in turn."""
        replies = iter(asked)

        def one(row, asking=False):
            got = dict(next(replies)) if asking else dict(row)
            return got, got, []

        typed = scripted(*lines)
        return edit_instances(self.SPEC, rows, one, typed), typed.prompts

    def test_nothing_yet_goes_straight_to_add(self, capsys):
        rows, prompts = self.edit([], "d", asked=[{"name": "a", "root": "/a"}])
        assert rows == [{"name": "a", "root": "/a"}]
        assert prompts == [MENU]
        assert 'a               root = "/a"' in capsys.readouterr().out

    def test_add_edit_remove(self):
        start = [{"name": "a", "root": "/a"}]
        asked = [{"name": "b", "root": "/b"}, {"name": "b", "root": "/c"}]
        rows, prompts = self.edit(start, "a", "e", "b", "r", "1", "d", asked=asked)
        assert rows == [
            {"name": "a", "root": "/a", "removed": True},
            {"name": "b", "root": "/c"},  # edited in place
        ]
        assert prompts == [MENU, MENU, "  choose: ", MENU, "  choose: ", MENU]

    def test_with_one_instance_there_is_nothing_to_choose(self):
        rows, prompts = self.edit([{"name": "a"}], "r", "d")
        assert rows == [{"name": "a", "removed": True}]
        assert prompts == [MENU, "\n[a]dd / [d]one: "]

    def test_a_new_key_is_a_new_instance(self):
        start = [{"name": "a", "root": "/a"}]
        rows, _ = self.edit(start, "e", "d", asked=[{"name": "z", "root": "/a"}])
        assert rows == [
            {"name": "a", "root": "/a", "removed": True},
            {"name": "z", "root": "/a"},
        ]

    def test_a_taken_key_is_refused(self, capsys):
        start = [{"name": "a", "root": "/a"}]
        rows, _ = self.edit(start, "a", "d", asked=[{"name": "a", "root": "/x"}])
        assert rows == start
        assert "there is already a site with name a" in capsys.readouterr().out

    def test_adding_one_back_cancels_its_removal(self):
        start = [{"name": "a", "root": "/a", "removed": True}, {"name": "b"}]
        rows, _ = self.edit(start, "a", "d", asked=[{"name": "a", "root": "/new"}])
        assert rows == [{"name": "b"}, {"name": "a", "root": "/new"}]

    def test_rows_with_a_missing_answer_are_asked_first(self):
        def one(row, asking=False):
            got = dict(row) | ({"port": 21} if asking else {})
            return got, got, [] if "port" in got else ["port?"]

        rows = edit_instances(self.SPEC, [{"name": "a"}], one, scripted("d"))
        assert rows == [{"name": "a", "port": 21}]


# ---------------------------------------------------------------- end to end


class Host:
    """The fake machine across several runs of the sites recipe."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.base = tmp_path / "base"
        self.units = {}
        self.shells = []

    def make_shell(self, log):
        sh = FakeShell(log)
        sh.units = self.units
        self.shells.append(sh)
        return sh

    def run(self, *lines, tty=True, **kwargs):
        typed = scripted(*lines)
        code = install_recipe(
            str(SITES), input_fn=typed, tty=tty, make_shell=self.make_shell, **kwargs
        )
        return code, self.shells[-1], typed.prompts

    def first(self):
        """One web site `a`, applied."""
        return self.run(str(self.base), "a", str(self.tmp / "a"), "", "d", "a")

    def sites(self):
        return load_answers("sites")["site"]


@pytest.fixture
def host(tmp_path, monkeypatch, me):
    monkeypatch.setitem(FACTS, "invoking_user", lambda _sh: me["owner"])
    monkeypatch.setitem(FACTS, "invoking_group", lambda _sh: me["group"])
    return Host(tmp_path)


class TestFlow:
    def test_first_run_adds_one_then_offers_the_menu(self, host, capsys):
        code, _, prompts = host.first()
        assert code == 0
        assert prompts[:3] == ["Where the index goes: ", "Site name: ", "Folder: "]
        assert prompts[-2:] == [MENU, "\n[a]pply / [e]xit: "]
        assert len(prompts) == 6  # the FTP port is not asked: ftp is not ticked
        assert host.sites() == [
            {"name": "a", "root": str(host.tmp / "a"), "serve": ["web"]}
        ]
        assert (host.tmp / "a" / "a.web").read_text() == f"[a]\nroot = {host.tmp}/a\n"
        assert host.units["web-a.service"]["active"]
        assert "ok  active: web-a.service" in capsys.readouterr().out

    def test_second_run_asks_nothing(self, host):
        host.first()
        code, sh, prompts = host.run()
        assert (code, prompts, sh.changes()) == (0, [], [])

    def test_not_a_terminal_lists_the_instance_questions(self, host, capsys):
        code, _, _ = host.run(tty=False)
        assert code == 1
        out = capsys.readouterr().out
        assert "base            Where the index goes" in out
        assert "name            Site name" in out

    def test_add_a_second_one(self, host, capsys):
        host.first()
        capsys.readouterr()
        b = host.tmp / "b"
        code, sh, prompts = host.run(
            "", "a", "b", str(b), "web,ftp", "2121", "d", "a", reconfigure=True
        )
        assert code == 0
        assert "FTP port [21]: " in prompts
        assert [s["name"] for s in host.sites()] == ["a", "b"]
        assert host.sites()[1]["port"] == "2121"
        assert (b / "b.ftp").read_text() == "ftp b on port 2121\n"
        index = (host.base / "index.conf").read_text()
        assert index.endswith(f"[a]\nroot = {host.tmp}/a\n[b]\nroot = {b}\n")
        assert "systemctl restart web-b.service" in sh.changes()
        assert "systemctl restart web-a.service" not in sh.changes()  # untouched

    def test_edit_rewrites_in_place(self, host):
        host.first()
        code, sh, prompts = host.run(
            "", "e", "", "", "web,ftp", "", "d", "a", reconfigure=True
        )
        assert code == 0
        assert prompts[2] == "Site name [a]: "  # saved answers are the defaults
        assert host.sites()[0]["serve"] == ["web", "ftp"]
        assert (host.tmp / "a" / "a.ftp").read_text() == "ftp a on port 21\n"
        assert not any(c.startswith("rm ") for c in sh.changes())

    def test_unticking_removes_only_that_part(self, host, capsys):
        host.first()
        host.run(
            "", "a", "b", str(host.tmp / "b"), "web,ftp", "", "d", "a", reconfigure=True
        )
        capsys.readouterr()
        code, sh, _ = host.run(
            "", "e", "b", "", "", "ftp", "", "d", "a", reconfigure=True
        )
        assert code == 0
        b = host.tmp / "b"
        assert not (b / "b.web").exists()
        assert (b / "b.ftp").exists()
        assert not host.units["web-b.service"]["active"]
        assert host.units["web-a.service"]["active"]
        changes = sh.changes()
        assert changes.index("systemctl disable --now web-b.service") < changes.index(
            f"rm -f {b}/b.web"
        )
        removed = [c for c in changes if c.startswith(("rm ", "rmdir ", "find "))]
        assert removed == [f"rm -f {b}/b.web"]
        assert "[b]" not in (host.base / "index.conf").read_text()
        assert "active: web-b.service" not in capsys.readouterr().out

    def test_remove_takes_away_what_the_instance_owned(self, host, capsys):
        host.first()
        b = host.tmp / "b"
        host.run("", "a", "b", str(b), "web,ftp", "", "d", "a", reconfigure=True)
        capsys.readouterr()
        code, sh, prompts = host.run("", "r", "b", "d", "a", reconfigure=True)
        assert code == 0
        assert "  choose: " in prompts
        assert not b.exists()
        assert (host.tmp / "a" / "a.web").exists()
        assert not host.units["web-b.service"]["active"]
        assert host.units["web-a.service"]["active"]
        touched = [c for c in sh.changes() if str(host.tmp / "a") in c]
        assert touched == []
        assert [s["name"] for s in host.sites()] == ["a"]  # forgotten once it is gone
        out = capsys.readouterr().out
        assert "(absent)" in out
        assert "ok  active: web-a.service" in out

        code, sh, prompts = host.run()
        assert (code, prompts, sh.changes()) == (0, [], [])

    def test_a_removal_that_was_not_applied_is_remembered(self, host, capsys):
        host.first()
        code, sh, _ = host.run("", "r", "d", "e", reconfigure=True)
        assert code == 0
        assert sh.changes() == []
        assert host.sites()[0]["removed"] is True
        assert (host.tmp / "a" / "a.web").exists()
        capsys.readouterr()

        code, sh, _ = host.run(tty=False)  # the check still plans the removal
        assert code == 0
        out = capsys.readouterr().out
        assert f"rm -f {host.tmp}/a/a.web" in out
        assert "Nothing was changed: stdin is not a terminal." in out

        code, sh, prompts = host.run("a")
        assert code == 0
        assert prompts == ["\n[a]pply / [e]xit: "]
        assert not (host.tmp / "a").exists()
        assert not (host.base / "index.conf").exists()  # nothing left to index
        assert "site" not in load_answers("sites") or host.sites() == []

    def test_whole_recipe_remove_keeps_the_answers(self, host):
        host.first()
        code, _, _ = host.run("a", remove=True)
        assert code == 0
        assert not (host.tmp / "a").exists()
        assert [s["name"] for s in host.sites()] == ["a"]
