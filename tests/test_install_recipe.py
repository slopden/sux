import http.server
import shutil
import threading

import pytest

from sux.install import recipe as recipe_mod
from sux.install.facts import FACTS, Facts
from sux.install.log import Fail
from sux.install.model import Ctx, Probe
from sux.install.recipe import (
    Values,
    build_resources,
    build_verify,
    builtin_recipes,
    check_requires,
    evaluate,
    expand,
    load_recipe,
    render,
    resolve,
)
from sux.install.resources import SECRET_MARK
from sux.install.verify import run_verify

from .conftest import DEMO

pytestmark = pytest.mark.usefixtures("sandbox")


@pytest.fixture
def facts(sh, monkeypatch, me):
    monkeypatch.setitem(FACTS, "invoking_user", lambda _sh: me["owner"])
    monkeypatch.setitem(FACTS, "invoking_group", lambda _sh: me["group"])
    monkeypatch.setitem(FACTS, "arch", lambda _sh: "amd64")
    monkeypatch.setitem(FACTS, "systemd", lambda _sh: 257)
    monkeypatch.setitem(FACTS, "tailscale_ips", lambda _sh: ["100.1.2.3", "fd7a::1"])
    return Facts(sh)


@pytest.fixture
def answers(tmp_path, sandbox, me):
    return {
        "root": str(tmp_path / "srv"),
        "unit_dir": str(sandbox / "units"),
        "user": me["owner"],
        "features": ["web"],
        "level": "info",
        "tls": False,
    }


def values_of(**items):
    values = Values(Facts(None))
    values.update(items)
    return values


class TestWhen:
    V = values_of(protocols=["smb", "s3"], tls=False, level="info", port=445)

    @pytest.mark.parametrize(
        ("expr", "want"),
        [
            ("'s3' in protocols", True),
            ("'nfs' in protocols", False),
            ("'nfs' not in protocols", True),
            ("tls", False),
            ("not tls", True),
            ("level == 'info'", True),
            ("level != 'info'", False),
            ("port == 445 and 'smb' in protocols", True),
            ("tls or level == 'debug'", False),
            ("tls or not ('nfs' in protocols)", True),
            ("level in ['info', 'debug']", True),
            ("level in ('warn',)", False),
            ("True", True),
            ("protocols == ['smb', 's3']", True),
        ],
    )
    def test_expressions(self, expr, want):
        assert bool(evaluate(expr, self.V)) is want

    @pytest.mark.parametrize(
        "expr",
        [
            "__import__('os').system('true')",
            "protocols.clear()",
            "protocols[0]",
            "port + 1",
            "port < 500",
            "[x for x in protocols]",
            "lambda: 1",
            "level if tls else port",
            "f'{level}'",
            "(tls := True)",
        ],
    )
    def test_anything_else_is_rejected(self, expr):
        with pytest.raises(Fail, match="only names, literals"):
            evaluate(expr, self.V)
        assert self.V["protocols"] == ["smb", "s3"]

    def test_errors_are_user_facing(self):
        with pytest.raises(Fail, match="unknown name 'nope'"):
            evaluate("nope", self.V)
        with pytest.raises(Fail, match="bad expression"):
            evaluate("'s3' in", self.V)
        with pytest.raises(Fail, match="iterable"):
            evaluate("'x' in port", self.V)

    def test_names_fall_back_to_facts(self, facts):
        assert evaluate("arch == 'amd64'", Values(facts)) is True


class TestRender:
    V = values_of(root="/srv", port=8080, tls=True, listen=["a", "b"])

    def test_substitution(self):
        assert render("$root:${port} $tls $listen $$x", self.V, "t") == (
            "/srv:8080 true a b $x"
        )

    def test_unknown_variable(self):
        with pytest.raises(Fail, match=r"step 1: unknown variable \$nope"):
            render("$nope", self.V, "step 1")

    def test_bad_placeholder(self):
        with pytest.raises(Fail, match=r"write \$\$ for a literal \$"):
            render("cost: $ 5", self.V, "t")

    def test_expand_keeps_types_and_splices_lists(self):
        step = {
            "port": "$port",
            "tls": "${tls}",
            "path": "$root/etc",
            "addresses": ["127.0.0.1", "$listen"],
            "nested": {"argv": ["run", "--root=$root"]},
            "mode": 0o644,
        }
        assert expand(step, self.V, "t") == {
            "port": 8080,
            "tls": True,
            "path": "/srv/etc",
            "addresses": ["127.0.0.1", "a", "b"],
            "nested": {"argv": ["run", "--root=/srv"]},
            "mode": 0o644,
        }


class TestLoading:
    def test_load_by_path(self):
        recipe = load_recipe(str(DEMO))
        assert recipe.name == "demo"
        assert recipe.description == "Example recipe for the tests"
        assert set(recipe.tree) == {
            "recipe.toml",
            "hooks.py",
            "templates/demo.conf",
            "templates/demo-web.service",
            "templates/listen.conf",
            "templates/metrics.conf",
        }
        assert len(recipe.sha256) == 64
        assert recipe.sha256 == load_recipe(str(DEMO)).sha256

    def test_hash_covers_templates(self, tmp_path):
        copy = tmp_path / "demo"
        shutil.copytree(DEMO, copy)
        assert load_recipe(str(copy)).sha256 == load_recipe(str(DEMO)).sha256
        (copy / "templates" / "listen.conf").write_text("changed\n")
        assert load_recipe(str(copy)).sha256 != load_recipe(str(DEMO)).sha256

    def test_builtin_by_name(self, monkeypatch):
        monkeypatch.setattr(recipe_mod, "RECIPES_DIR", DEMO.parent)
        assert list(builtin_recipes()) == ["demo"]
        assert load_recipe("demo").tree == load_recipe(str(DEMO)).tree

    def test_inlined_trees_win(self, monkeypatch):
        tree = {"recipe.toml": '[recipe]\nname = "inline"\n'}
        monkeypatch.setattr(recipe_mod, "BUILTIN_RECIPES", {"inline": tree})
        assert load_recipe("inline").name == "inline"

    def test_unknown_recipe(self, monkeypatch):
        monkeypatch.setattr(recipe_mod, "RECIPES_DIR", DEMO.parent)
        with pytest.raises(Fail, match=r"unknown recipe 'nope' \(available: demo\)"):
            load_recipe("nope")

    def test_bad_toml(self, tmp_path):
        (tmp_path / "recipe.toml").write_text("[recipe\n")
        with pytest.raises(Fail, match="recipe.toml"):
            load_recipe(str(tmp_path))

    def test_hooks_get_the_engine_names(self):
        hooks = load_recipe(str(DEMO)).hooks()
        assert set(hooks) >= {"marker", "check_conf"}
        assert "Path" in hooks  # public names of the module; harmless


class TestResolve:
    def test_unanswered_questions_are_returned_in_order(self, facts, me):
        recipe = load_recipe(str(DEMO))
        values, answers, unanswered = resolve(recipe, facts, {})
        assert answers == {}
        assert [q.name for q in unanswered] == [
            "root",
            "unit_dir",
            "user",
            "features",
            "level",
            "tls",
        ]
        by_name = {q.name: q for q in unanswered}
        assert by_name["root"].default is None
        assert by_name["root"].kind == "path"
        assert by_name["user"].default == me["owner"]  # from a fact
        assert by_name["user"].kind == "text"
        assert by_name["features"].choices == ["web", "metrics"]
        assert by_name["features"].default == ["web"]
        assert by_name["tls"].default is False
        assert values["port"] == 8080

    def test_saved_answers_are_not_asked(self, facts, answers):
        asked = []
        recipe = load_recipe(str(DEMO))
        values, got, unanswered = resolve(recipe, facts, answers, ask=asked.append)
        assert asked == []
        assert unanswered == []
        assert got == answers
        assert values["conf"] == f"{answers['root']}/etc/demo.conf"  # derived var
        assert values["listen"] == ["127.0.0.1", "::1"]

    def test_only_new_questions_are_asked(self, facts, answers):
        del answers["level"]
        asked = []

        def ask(q):
            asked.append(q.name)
            return "debug"

        _, got, _ = resolve(load_recipe(str(DEMO)), facts, answers, ask=ask)
        assert asked == ["level"]
        assert got["level"] == "debug"

    def test_reconfigure_asks_again_with_saved_defaults(self, facts, answers):
        answers["level"] = "debug"
        seen = {}

        def ask(q):
            seen[q.name] = q.default
            return q.default

        resolve(load_recipe(str(DEMO)), facts, answers, ask=ask, reconfigure=True)
        assert seen == answers

    def test_choices_can_come_from_a_fact(self, facts):
        recipe = load_recipe(str(DEMO))
        recipe.data["vars"] = {
            "listen": {"ask": "Listen on", "kind": "multi", "fact": "tailscale_ips"},
            "one": {"ask": "Pick", "kind": "choice", "fact": "tailscale_ips"},
        }
        _, _, (listen, one) = resolve(recipe, facts, {})
        assert listen.choices == ["100.1.2.3", "fd7a::1"]
        assert listen.default == ["100.1.2.3", "fd7a::1"]
        assert one.choices == ["100.1.2.3", "fd7a::1"]
        assert one.default is None

    def test_bad_declarations(self, facts):
        recipe = load_recipe(str(DEMO))
        recipe.data["vars"] = {"x": {"ask": "X", "kind": "slider"}}
        with pytest.raises(Fail, match="kind must be one of"):
            resolve(recipe, facts, {})
        recipe.data["vars"] = {"x": {"ask": "X", "kind": "choice"}}
        with pytest.raises(Fail, match="needs `choices`"):
            resolve(recipe, facts, {})
        recipe.data["vars"] = {"x": {"fact": "shoe_size"}}
        with pytest.raises(Fail, match="unknown fact 'shoe_size'"):
            resolve(recipe, facts, {})

    def test_requires(self, facts, monkeypatch):
        recipe = load_recipe(str(DEMO))
        check_requires(recipe, facts)
        recipe.data["recipe"]["requires"] = {"systemd": ">=257", "arch": ["amd64"]}
        check_requires(recipe, facts)
        recipe.data["recipe"]["requires"] = {"systemd": ">=258"}
        with pytest.raises(Fail, match="requires systemd >=258; this is 257"):
            check_requires(recipe, facts)
        monkeypatch.setitem(FACTS, "arch", lambda _sh: "riscv64")
        recipe.data["recipe"]["requires"] = {"arch": ["amd64"]}
        with pytest.raises(Fail, match="requires arch"):
            check_requires(recipe, Facts(None))


class TestSteps:
    def build(self, facts, answers, **kwargs):
        recipe = load_recipe(str(DEMO))
        values, _, _ = resolve(recipe, facts, answers)
        return recipe, values, build_resources(recipe, values, **kwargs)

    def test_order_and_implicit_daemon_reload(self, facts, answers):
        _, _, res = self.build(facts, answers)
        assert [(r.kind, r.present) for r in res] == [
            ("dir", True),
            ("file", True),
            ("file", True),
            ("systemd", True),  # between the unit file and the unit
            ("unit", True),
            ("python", True),
        ]
        assert res[3].units == ["demo-web.service"]

    def test_template_with_fragments(self, facts, answers, me):
        _, _, res = self.build(facts, answers | {"tls": True})
        assert res[1].content == (
            "# Managed by sux install.\n"
            f"root = {answers['root']}\n"
            f"user = {me['owner']}\n"
            "level = info\n"
            "tls = true\n"
            "listen = 127.0.0.1:8080\n"
            "listen = ::1:8080\n"
            "price = $5\n"  # the unticked fragment's line is gone, `$$` is `$`
        )
        _, _, res = self.build(facts, answers | {"features": ["web", "metrics"]})
        assert "listen = ::1:8080\n[metrics]\nenabled = true\nprice" in res[1].content
        assert f"ExecStart=/usr/bin/demo --config {res[1].path}\n" in res[2].content
        assert res[4].restart_on == [str(res[1].path), str(res[2].path)]

    def test_when_false_plans_removal_first(self, facts, answers):
        _, _, res = self.build(facts, answers | {"features": []})
        assert [(r.kind, r.present) for r in res] == [
            ("unit", False),  # last step first: stop before deleting the file
            ("file", False),
            ("dir", True),
            ("file", True),
            ("python", True),
            ("systemd", True),
        ]

    def test_remove_inverts_every_step(self, facts, answers):
        _, _, res = self.build(facts, answers, remove=True)
        assert [(r.kind, r.present) for r in res] == [
            ("python", False),
            ("unit", False),
            ("file", False),
            ("file", False),
            ("dir", False),
            ("systemd", True),
        ]

    def test_keep_exempts_a_step_from_removal(self, facts, answers):
        recipe = load_recipe(str(DEMO))
        recipe.data["step"][0]["keep"] = True
        values, _, _ = resolve(recipe, facts, answers)
        kinds = [r.kind for r in build_resources(recipe, values, remove=True)]
        assert "dir" not in kinds
        assert "dir" in [r.kind for r in build_resources(recipe, values)]

    def test_secret_steps_get_the_placeholder(self, facts, answers):
        recipe = load_recipe(str(DEMO))
        recipe.data["step"] = [
            {"kind": "secret", "path": "$root/key", "content": "KEY=$secret\n"}
        ]
        values, _, _ = resolve(recipe, facts, answers)
        (res,) = build_resources(recipe, values)
        assert res.content == f"KEY={SECRET_MARK}\n"

    def test_errors_name_the_step(self, facts, answers):
        recipe = load_recipe(str(DEMO))
        values, _, _ = resolve(recipe, facts, answers)
        recipe.data["step"] = [{"kind": "teleport"}]
        with pytest.raises(Fail, match=r"step 1 \(teleport\): unknown kind"):
            build_resources(recipe, values)
        recipe.data["step"] = [{"kind": "file", "content": "x"}]
        with pytest.raises(Fail, match=r"step 1 \(file\): missing key 'path'"):
            build_resources(recipe, values)
        recipe.data["step"] = [{"kind": "file", "path": "/x", "template": "nope"}]
        with pytest.raises(Fail, match="no template 'nope'"):
            build_resources(recipe, values)
        recipe.data["step"] = [{"kind": "dir", "path": "$nope"}]
        with pytest.raises(Fail, match=r"unknown variable \$nope"):
            build_resources(recipe, values)

    def test_hook_step(self, facts, answers, sh, tmp_path):
        recipe, values, res = self.build(facts, answers)
        ctx = Ctx(sh, "demo", hooks=recipe.hooks(), values=values)
        (tmp_path / "srv").mkdir()
        probe = res[-1].probe(ctx)
        assert probe == Probe(False, "missing")
        (action,) = res[-1].plan(ctx, probe)
        assert action.argv == ["touch", f"{answers['root']}/marker"]


class TestVerify:
    def ctx(self, sh, **hooks):
        return Ctx(sh, "demo", hooks=hooks, values={})

    def test_entries_follow_when(self, facts, answers):
        recipe = load_recipe(str(DEMO))
        values, _, _ = resolve(recipe, facts, answers)
        assert [e["kind"] for e in build_verify(recipe, values)] == [
            "unit_active",
            "python",
        ]
        values["features"] = []
        assert [e["kind"] for e in build_verify(recipe, values)] == ["python"]

    def test_unit_active(self, sh, capsys):
        entry = {"kind": "unit_active", "names": ["a.service", "b.socket"]}
        sh.unit("a.service")
        with pytest.raises(Fail, match="b.socket is inactive"):
            run_verify([entry], self.ctx(sh))
        sh.unit("b.socket")
        run_verify([entry], self.ctx(sh))
        assert "ok  active: a.service, b.socket" in capsys.readouterr().out

    def test_listeners(self, sh):
        sh.replies["ss -ltnH"] = (
            0,
            "LISTEN 0 50 127.0.0.1:445 0.0.0.0:*\n"
            "LISTEN 0 50 [::1]:445 [::]:*\n"
            "LISTEN 0 50 100.1.2.3%tailscale0:445 0.0.0.0:*\n"
            "LISTEN 0 50 0.0.0.0:22 0.0.0.0:*\n",
        )
        entry = {
            "kind": "listeners",
            "port": 445,
            "addresses": ["127.0.0.1", "::1", "100.1.2.3"],
        }
        run_verify([entry], self.ctx(sh))
        entry["addresses"] = ["127.0.0.1", "::1"]
        with pytest.raises(Fail, match="port 445 listens on"):
            run_verify([entry], self.ctx(sh))
        with pytest.raises(Fail, match=r"port 22 listens on \['0.0.0.0:22'\]"):
            run_verify([entry | {"port": 22}], self.ctx(sh))

    def test_listeners_can_demand_a_closed_port(self, sh, capsys):
        sh.replies["ss -ltnH"] = (0, "LISTEN 0 50 127.0.0.1:445 0.0.0.0:*\n")
        run_verify([{"kind": "listeners", "port": 139, "addresses": []}], self.ctx(sh))
        assert "ok  nothing listens on port 139" in capsys.readouterr().out
        with pytest.raises(Fail, match=r"port 445 listens on .*, expected \[\]"):
            run_verify([{"kind": "listeners", "port": 445, "addresses": []}], self.ctx(sh))

    def test_command(self, sh):
        sh.replies["tool --check"] = (0, "all good\n")
        entry = {"kind": "command", "argv": ["tool", "--check"], "contains": "good"}
        run_verify([entry], self.ctx(sh))
        with pytest.raises(Fail, match="output lacks 'bad'"):
            run_verify([entry | {"contains": "bad"}], self.ctx(sh))
        with pytest.raises(Fail, match="exited 0"):
            run_verify([entry | {"rc": 1}], self.ctx(sh))
        with pytest.raises(Fail, match="needs sudo"):
            run_verify([entry | {"sudo": True}], self.ctx(sh))

    def test_http_ok(self, sh):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == "/health" else 404)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            entry = {"kind": "http_ok", "url": f"{base}/health", "timeout": 5}
            run_verify([entry], self.ctx(sh))
            missing = {"kind": "http_ok", "url": f"{base}/nope", "timeout": 0}
            with pytest.raises(Fail, match="-> 404, expected 200"):
                run_verify([missing], self.ctx(sh))
            run_verify([missing | {"status": 404}], self.ctx(sh))
        finally:
            server.shutdown()
            server.server_close()

    def test_python(self, sh):
        def healthy(ctx, entry):
            return f"checked {entry['what']}"

        def sick(ctx, entry):
            raise Fail("it is down")

        entry = {"kind": "python", "function": "healthy", "what": "it"}
        run_verify([entry], self.ctx(sh, healthy=healthy))
        with pytest.raises(Fail, match="it is down"):
            run_verify([entry | {"function": "sick"}], self.ctx(sh, sick=sick))
        with pytest.raises(Fail, match="no function 'healthy'"):
            run_verify([entry], self.ctx(sh))

    def test_unknown_kind_and_missing_key(self, sh):
        with pytest.raises(Fail, match="unknown kind 'vibes'"):
            run_verify([{"kind": "vibes"}], self.ctx(sh))
        with pytest.raises(Fail, match="verify listeners: missing key 'port'"):
            run_verify([{"kind": "listeners"}], self.ctx(sh))
