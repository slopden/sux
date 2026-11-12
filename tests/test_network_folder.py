"""The network-folder recipe: expansion, rendering and its hooks.

Nothing here probes or changes the machine: resources are only built, and the
hooks run against a scripted Shell, temporary folders and a local fake S3.
"""

import http.server
import importlib.util
import os
import re
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

import build_single_file
from sux.install.facts import FACTS, Facts
from sux.install.log import Fail
from sux.install.model import Ctx
from sux.install.recipe import (
    Values,
    build_resources,
    build_status,
    build_verify,
    builtin_recipes,
    load_recipe,
    managed_units,
    resolve,
)
from sux.install.resources import SECRET_MARK

pytestmark = pytest.mark.usefixtures("sandbox")

# The two scripts this recipe replaces, as the reference for what it renders:
#   SUX_REFERENCE_INSTALLERS=<their directory> make test
REFERENCE = os.environ.get("SUX_REFERENCE_INSTALLERS", "")
OLD_SCRIPTS = [Path(REFERENCE) / f"install_{n}.py" for n in ("samba", "versitygw")]
HAVE_REFERENCE = bool(REFERENCE) and all(p.is_file() for p in OLD_SCRIPTS)

UNITS = "/etc/systemd/system"
IPS = ["100.64.0.7", "fd7a::7"]


@pytest.fixture
def facts(sh, monkeypatch):
    monkeypatch.setitem(FACTS, "invoking_user", lambda _sh: "alice")
    monkeypatch.setitem(FACTS, "invoking_group", lambda _sh: "staff")
    monkeypatch.setitem(FACTS, "tailscale_ips", lambda _sh: list(IPS))
    monkeypatch.setitem(FACTS, "magicdns", lambda _sh: "box.example.ts.net")
    return Facts(sh)


@pytest.fixture
def recipe():
    return load_recipe("network-folder")


def folder(share, protocols=("smb", "s3"), **more):
    row = {
        "root": f"/srv/{share}",
        "share": share,
        "user": "alice",
        "group": "staff",
        "listen": list(IPS),
        "protocols": list(protocols),
    }
    return row | ({"port": 7070} if "s3" in protocols else {}) | more


def build(recipe, facts, *rows, **kwargs):
    values, _, unanswered = resolve(recipe, facts, {"folder": list(rows)})
    assert unanswered == []
    return {r.key: r for r in build_resources(recipe, values, **kwargs)}


def present(resources, wanted=True):
    return [key for key, r in resources.items() if r.present is wanted]


def s3_keys(share):
    """Everything one folder's S3 service owns."""
    unit = f"{UNITS}/versitygw-{share}"
    return [
        f"secret:/etc/versitygw-{share}.env",
        f"dir:/var/lib/versitygw-{share}",
        f"dir:/var/lib/versitygw-{share}/root",
        f"file:{unit}.service",
        f"file:{unit}-health.service",
        f"file:{unit}-health.timer",
        f"file:{unit}-janitor.service",
        f"file:{unit}-janitor.timer",
        f"unit:versitygw-{share}.service",
        f"unit:versitygw-{share}-health.timer",
        f"unit:versitygw-{share}-janitor.timer",
    ]


VGWLOCKS = "dir:/srv/{}/.vgwlocks"  # only ever cleaned up, never created
SMB_KEYS = [
    "file:/etc/samba/smb.conf",
    f"dir:{UNITS}/smbd.service.d",
    f"file:{UNITS}/smbd.service.d/tailscale.conf",
    f"file:{UNITS}/smbd-tailnet.socket",
    f"file:{UNITS}/smbd-tailnet.service",
    "apt:samba smbclient",
    "unit:nmbd",
    "unit:smbd",
    "unit:smbd-tailnet.socket",
    "unit:smbd-tailnet.service",
]


# ---------------------------------------------------------------- the recipe


class TestRecipe:
    def test_is_built_in(self, recipe):
        assert "network-folder" in builtin_recipes()
        assert recipe.instances["name"] == "folder"
        assert recipe.data["recipe"]["requires"] == {
            "systemd": ">=257",
            "arch": ["amd64"],
        }

    def test_questions_for_a_folder(self, recipe, facts):
        _, _, unanswered = resolve(recipe, facts, {})
        asked = {q.name: (q.kind, q.default) for q in unanswered}
        assert asked == {
            "root": ("path", None),
            "share": ("text", None),
            "user": ("text", "alice"),  # defaults come from facts
            "group": ("text", "staff"),
            "listen": ("multi", IPS),  # tailscale only
            "protocols": ("multi", ["smb"]),
        }
        only_s3 = {"folder": [folder("a", ["s3"]) | {"port": None}]}
        del only_s3["folder"][0]["port"]
        _, _, unanswered = resolve(recipe, facts, only_s3)
        assert [(q.name, q.default) for q in unanswered] == [("port", 7070)]

    def test_one_folder_over_both(self, recipe, facts):
        resources = build(recipe, facts, folder("a"))
        assert present(resources, wanted=False) == []
        assert (
            present(resources)
            == [
                "python:preflight a",
                *SMB_KEYS[:6],  # smb.conf is written before the package is installed
                "deb:versitygw",
                *s3_keys("a")[:8],
                "systemd:daemon-reload",  # after every unit file, before any start
                "unit:nmbd",
                "python:Samba password for alice",
                *SMB_KEYS[7:],
                *s3_keys("a")[8:],
            ]
        )
        assert resources["unit:nmbd"].state == "masked"
        assert resources["unit:smbd-tailnet.service"].state == "on-demand"
        assert resources["apt:samba smbclient"].no_autostart is True
        smbd = resources["unit:smbd"]
        assert smbd.validate == ["testparm", "-s", "/etc/samba/smb.conf"]
        assert "apt:samba smbclient" in smbd.restart_on
        deb = resources["deb:versitygw"]
        assert deb.url.endswith("/v1.10.0/versitygw_1.10.0_linux_amd64.deb")
        assert len(deb.sha256) == 64
        secret = resources["secret:/etc/versitygw-a.env"]
        assert secret.meta == "root:root 0600"
        assert secret.content.endswith(
            f"\nROOT_ACCESS_KEY_ID=a\nROOT_SECRET_ACCESS_KEY={SECRET_MARK}\n"
        )
        assert resources["dir:/var/lib/versitygw-a/root"].empty_tree is True
        assert resources["unit:versitygw-a.service"].restart_on == [
            f"{UNITS}/versitygw-a.service",
            "/etc/versitygw-a.env",
            "/usr/bin/versitygw",
            "deb:versitygw",
            "dir:/var/lib/versitygw-a/root",
        ]

    def test_smb_only_leaves_the_s3_half_absent(self, recipe, facts):
        resources = build(recipe, facts, folder("a", ["smb"]))
        assert present(resources) == [
            "python:preflight a",
            *SMB_KEYS[:6],
            "systemd:daemon-reload",
            "unit:nmbd",
            "python:Samba password for alice",
            *SMB_KEYS[7:],
        ]
        assert sorted(present(resources, wanted=False)) == sorted(
            ["deb:versitygw", *s3_keys("a"), VGWLOCKS.format("a")]
        )

    def test_s3_only_leaves_the_smb_half_absent(self, recipe, facts):
        resources = build(recipe, facts, folder("a", ["s3"]))
        gone = present(resources, wanted=False)
        assert sorted(gone) == sorted([*SMB_KEYS, "python:Samba password for alice"])
        assert gone[0] == "unit:smbd-tailnet.service"  # stopped before files go
        assert gone[-1] == "file:/etc/samba/smb.conf"
        assert present(resources)[:2] == ["python:preflight a", "deb:versitygw"]

    def test_two_folders_share_samba_and_get_their_own_s3(self, recipe, facts):
        b = folder("b", port=7071, listen=["100.64.0.7", "192.0.2.9"], user="bob")
        resources = build(recipe, facts, folder("a"), b)
        assert present(resources, wanted=False) == []
        assert len(resources) == len(SMB_KEYS) + 2 * len(s3_keys("a")) + 6
        conf = resources["file:/etc/samba/smb.conf"].content
        assert conf.count("[global]") == 1
        assert "\n\n[a]\n    path = /srv/a\n    valid users = alice\n" in conf
        assert "\n\n[b]\n    path = /srv/b\n    valid users = bob\n" in conf
        assert conf.index("[a]") < conf.index("[b]")
        socket = resources[f"file:{UNITS}/smbd-tailnet.socket"].content
        assert re.findall(r"^ListenStream=(.*)$", socket, re.MULTILINE) == [
            "100.64.0.7:445",  # the union of what the SMB folders listen on
            "[fd7a::7]:445",
            "192.0.2.9:445",
        ]
        unit = resources[f"file:{UNITS}/versitygw-b.service"].content
        assert "User=bob\n" in unit
        assert "EnvironmentFile=/etc/versitygw-b.env\n" in unit
        assert "RootDirectory=/var/lib/versitygw-b/root\n" in unit
        assert re.findall(r"--port=(\S+)", unit) == [
            "100.64.0.7:7071",
            "192.0.2.9:7071",
            "127.0.0.1:7071",
        ]
        assert "python:Samba password for bob" in resources

    def test_unticking_s3_on_one_folder_removes_just_its_service(self, recipe, facts):
        resources = build(recipe, facts, folder("a"), folder("b", ["smb"]))
        assert present(resources, wanted=False) == [
            *reversed(s3_keys("b")),
            VGWLOCKS.format("b"),  # removed only if it is there and empty
        ]
        assert not any(".vgwlocks" in key for key in present(resources))
        assert resources["deb:versitygw"].present  # a still serves S3
        assert "[b]" in resources["file:/etc/samba/smb.conf"].content

    def test_unticking_smb_on_one_folder_only_drops_its_share(self, recipe, facts):
        b = folder("b", ["s3"], port=7071, user="bob")
        resources = build(recipe, facts, folder("a"), b)
        assert present(resources, wanted=False) == ["python:Samba password for bob"]
        assert "[b]" not in resources["file:/etc/samba/smb.conf"].content

    def test_removing_a_folder_removes_just_what_it_owned(self, recipe, facts):
        gone = folder("b", port=7071, user="bob") | {"removed": True}
        resources = build(recipe, facts, folder("a"), gone)
        assert (
            present(resources, wanted=False)
            == [
                *reversed(s3_keys("b")[8:]),  # its units stop first
                "python:Samba password for bob",
                *reversed(s3_keys("b")[:8]),
                VGWLOCKS.format("b"),
            ]
        )
        assert "python:preflight b" not in resources  # nothing to check any more
        assert "[b]" not in resources["file:/etc/samba/smb.conf"].content
        both = build(recipe, facts, folder("a"), folder("b", port=7071, user="bob"))
        kept = [k for k in both if "-b" not in k and "bob" not in k and " b" not in k]
        assert present(resources) == kept

    def test_removing_the_last_folder_removes_everything(self, recipe, facts):
        resources = build(recipe, facts, folder("a") | {"removed": True})
        assert present(resources) == ["systemd:daemon-reload (last)"]
        assert resources["unit:nmbd"].present is False  # unmasked again
        assert build(recipe, facts, folder("a"), remove=True).keys() == resources.keys()

    def test_conditional_sections_of_the_service_unit(self, recipe, facts):
        key = f"file:{UNITS}/versitygw-a.service"
        unit = build(recipe, facts, folder("a"))[key].content
        assert "    --keep-alive \\\n    posix \\\n" in unit
        assert "LimitNOFILE=1048576\n# access log disabled\n\n# Sandbox" in unit
        assert "RootDirectory=" in unit
        assert "ProtectSystem=strict" not in unit

        recipe.data["vars"] |= {
            "keep_alive": False,
            "read_only": "yes",
            "access_log": True,
            "minimal_root": False,
            "health_check": False,
        }
        resources = build(recipe, facts, folder("a"))
        unit = resources[key].content
        assert (
            "    --quiet \\\n    --readonly \\\n"
            "    --access-log=/var/log/versitygw-a/access.log \\\n    posix \\\n"
        ) in unit
        assert "LimitNOFILE=1048576\nLogsDirectory=versitygw-a\n\n# Sandbox" in unit
        assert (
            "under /srv/a.\nProtectSystem=strict\nReadWritePaths=/srv/a\n"
            "ProtectHome=yes\nPrivateTmp=yes\n# Own PID namespace"
        ) in unit
        assert "RootDirectory=" not in unit
        assert "read only = yes" in resources["file:/etc/samba/smb.conf"].content
        assert sorted(present(resources, wanted=False)) == sorted(
            [
                "dir:/var/lib/versitygw-a",
                "dir:/var/lib/versitygw-a/root",
                f"file:{UNITS}/versitygw-a-health.service",
                f"file:{UNITS}/versitygw-a-health.timer",
                "unit:versitygw-a-health.timer",
            ]
        )

    def test_health_checks(self, recipe, facts):
        rows = [folder("a"), folder("b", ["smb"]), folder("c") | {"removed": True}]
        values, _, _ = resolve(recipe, facts, {"folder": rows})
        entries = build_verify(recipe, values)
        summary = [(e["kind"], e.get("function") or e.get("port")) for e in entries]
        assert summary == [
            ("unit_active", None),
            ("listeners", 445),
            ("listeners", 139),
            ("python", "smb_anonymous"),
            ("python", "smb_login"),  # a
            ("python", "smb_login"),  # b
            ("http_ok", None),
            ("python", "s3_smoke"),
            ("python", "s3_sandbox"),
            ("python", "s3_client_help"),
        ]
        assert entries[1]["addresses"] == ["127.0.0.1", "::1", *IPS]
        assert entries[2]["addresses"] == []
        assert entries[6]["url"] == "http://127.0.0.1:7070/health"

    def test_status_snapshot_adds_testparm(self, recipe, facts):
        values, _, _ = resolve(recipe, facts, {"folder": [folder("a")]})
        assert build_status(recipe, values) == [
            {"argv": ["testparm", "-s", "/etc/samba/smb.conf"]}
        ]
        units = managed_units(build_resources(recipe, values))
        assert units == [
            "nmbd",
            "smbd",
            "smbd-tailnet.socket",
            "smbd-tailnet.service",
            "versitygw-a.service",
            "versitygw-a-health.timer",
            "versitygw-a-janitor.timer",
        ]
        values, _, _ = resolve(recipe, facts, {"folder": [folder("a", ["s3"])]})
        assert build_status(recipe, values) == []

    def test_every_hook_the_recipe_names_exists(self, recipe):
        hooks = recipe.hooks()
        named = recipe.data["step"] + recipe.data["verify"]
        wanted = {e["function"] for e in named if e["kind"] == "python"}
        assert wanted <= set(hooks)
        assert len(wanted) == 7


# ---------------------------------------------------------------- reference


def load_script(path):
    spec = importlib.util.spec_from_file_location(f"reference_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up
    spec.loader.exec_module(module)
    return module


def after_header(text):
    """A rendered file without its first line (the "managed by" comment)."""
    head, _, rest = text.partition("\n")
    assert head.startswith("# Managed by ")
    return rest


@pytest.mark.skipif(not HAVE_REFERENCE, reason="SUX_REFERENCE_INSTALLERS is not set")
class TestSameAsTheOldScripts:
    """Answered with the old scripts' settings, the recipe renders their files."""

    @pytest.fixture(scope="class")
    def old(self):
        samba, versitygw = (load_script(p) for p in OLD_SCRIPTS)
        return samba, versitygw, samba.S, versitygw.SETTINGS

    @pytest.fixture
    def resources(self, old, recipe, facts):
        _, _, smb, s3 = old
        assert (smb.root, smb.user, smb.group) == (s3.root, s3.user, s3.group)
        ips = [ip for ip in (s3.tailscale_ip, s3.tailscale_ip6) if ip]
        assert list(smb.tailscale_ips) == ips
        row = {
            "root": str(smb.root),
            "share": smb.share,
            "user": smb.user,
            "group": smb.group,
            "listen": ips,
            "protocols": ["smb", "s3"],
            "port": s3.port,
        }
        return build(recipe, facts, row)

    def expected(self, old):
        samba, versitygw, smb, s3 = old
        files = {
            samba.SMB_CONF: samba.render_conf(smb),
            samba.DROPIN: samba.DROPIN_TEXT,
            samba.SOCKET: samba.render_socket(smb),
            samba.PROXY: samba.render_proxy(smb),
            s3.unit_path: versitygw.render_unit(s3),
        }
        aux = versitygw.render_aux_units(s3)
        return files | {versitygw.UNIT_DIR / name: text for name, text in aux.items()}

    def test_every_file_is_identical_after_the_header_line(self, old, resources):
        expected = self.expected(old)
        assert len(expected) == 9
        for path, text in expected.items():
            ours = resources[f"file:{path}"]
            assert after_header(ours.content) == after_header(text), path
            assert ours.meta == "root:root 0644"
        files = [k for k in resources if k.startswith("file:")]
        assert len(files) == len(expected)  # and the recipe writes no other file

    def test_credentials_file(self, old, resources):
        _, versitygw, _, s3 = old
        ours = resources[f"secret:{s3.credentials_file}"]
        theirs = versitygw.render_credentials(s3.access_key_id, "S" * 43)
        assert after_header(ours.content.replace(SECRET_MARK, "S" * 43)) == (
            after_header(theirs)
        )
        assert ours.meta == "root:root 0600"

    def test_names_packages_and_paths(self, old, resources):
        samba, versitygw, smb, s3 = old
        deb = resources["deb:versitygw"]
        assert (deb.url, deb.sha256, deb.version) == (
            s3.deb_url,
            s3.deb_sha256,
            s3.version,
        )
        assert resources[f"dir:{s3.chroot_dir}"].meta == "root:root 0755"
        assert resources[f"apt:{' '.join(smb.packages)}"].present
        units = [f"{s3.service}.service", *s3.aux_units[:2]]
        units += ["smbd", "nmbd", samba.SOCKET.name, samba.PROXY.name]
        assert {f"unit:{u}" for u in units} == {
            k for k in resources if k.startswith("unit:")
        }
        watch = resources[f"unit:{s3.service}.service"].restart_on
        assert watch[:3] == [
            str(s3.unit_path),
            str(s3.credentials_file),
            str(versitygw.BINARY),
        ]

    def test_nothing_of_that_host_is_in_the_recipe(self, old, recipe):
        _, _, smb, s3 = old
        private = [smb.user, smb.group, smb.magicdns, *smb.tailscale_ips]
        private += [str(smb.root), smb.magicdns.split(".")[0]]
        for name, text in recipe.tree.items():
            for value in private:
                assert not re.search(rf"\b{re.escape(value)}\b", text), (name, value)


# ---------------------------------------------------------------- hooks


@pytest.fixture
def hooks(recipe):
    return recipe.hooks()


@pytest.fixture
def hctx(sh, facts):
    values = Values(facts)
    values["folder"] = []
    return Ctx(sh, "network-folder", values=values)


LISTENING = (
    "LISTEN 0 50 127.0.0.1:445 0.0.0.0:*\n"
    "LISTEN 0 50 [::1]:445 [::]:*\n"
    "LISTEN 0 4096 100.64.0.7:445 0.0.0.0:*\n"
    "LISTEN 0 4096 [fd7a::7]:445 [::]:*\n"
    "LISTEN 0 128 100.64.0.7:7070 0.0.0.0:*\n"
    "LISTEN 0 128 127.0.0.1:7070 0.0.0.0:*\n"
)


class TestPreflight:
    @pytest.fixture
    def step(self, tmp_path, me, sh):
        sh.replies["ip -o addr show to"] = (0, "4: tailscale0    inet 100.64.0.7/32")
        sh.replies["ss -ltnH"] = (0, LISTENING)
        return {
            "share": "a",
            "root": str(tmp_path),
            "user": me["owner"],
            "group": me["group"],
            "listen": list(IPS),
            "protocols": ["smb", "s3"],
            "port": 7070,
            "service": "versitygw-a",
            "smb_listen": list(IPS),
            "health_check": True,
        }

    def test_passes_on_a_host_that_already_runs_it(self, hooks, hctx, step, sh, me):
        sh.unit("versitygw-a")
        probe = hooks["preflight"](hctx, step, True)
        assert probe.ok is True
        assert probe.have.startswith(f"{me['owner']}(")
        assert probe.have.endswith(
            "addresses on tailscale0, port 7070 held by versitygw-a"
        )

    def test_reports_a_free_port(self, hooks, hctx, step, sh):
        sh.replies["ss -ltnH"] = (0, "")
        assert hooks["preflight"](hctx, step, True).have.endswith("port 7070 free")

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ({"share": "my share"}, "share name 'my share'"),
            ({"root": "/nonexistent/x"}, "/nonexistent/x is not a directory"),
            ({"group": "no-such-group-xyz"}, "missing user/group"),
            ({"listen": []}, "SMB needs at least one address"),
            ({"port": "http"}, "S3 port 'http' is not a number"),
        ],
    )
    def test_stops_on_answers_that_cannot_work(
        self, hooks, hctx, step, change, message
    ):
        with pytest.raises(Fail, match=message):
            hooks["preflight"](hctx, step | change, True)

    def test_stops_when_an_address_is_down(self, hooks, hctx, step, sh):
        sh.replies["ip -o addr show to"] = (0, "")
        with pytest.raises(Fail, match="100.64.0.7 is not on any interface"):
            hooks["preflight"](hctx, step, True)

    def test_stops_when_smb_ports_belong_to_something_else(self, hooks, hctx, step, sh):
        sh.replies["ss -ltnH"] = (0, LISTENING + "LISTEN 0 50 0.0.0.0:139 0.0.0.0:*\n")
        with pytest.raises(Fail, match=r"SMB ports are in use .*'0.0.0.0:139'"):
            hooks["preflight"](hctx, step, True)

    def test_stops_when_the_s3_port_belongs_to_something_else(self, hooks, hctx, step):
        with pytest.raises(Fail, match=r"already in use by something else: \['100"):
            hooks["preflight"](hctx, step, True)  # versitygw-a is not running

    def test_stops_when_two_folders_want_one_port(self, hooks, hctx, step):
        both = {"protocols": ["s3"], "port": "7070"}
        hctx.values["folder"] = [both, both | {"port": 7070}]
        with pytest.raises(Fail, match="more than one folder serves S3 on port 7070"):
            hooks["preflight"](hctx, step, True)

    def test_stops_without_curl_for_the_health_check(self, hooks, hctx, step, sh):
        sh.replies["curl --version"] = (127, "")
        with pytest.raises(Fail, match="needs curl"):
            hooks["preflight"](hctx, step, True)
        sh.unit("versitygw-a")
        assert hooks["preflight"](hctx, step | {"health_check": False}, True).ok

    def test_warns_about_directories_that_cannot_be_buckets(
        self, hooks, hctx, step, sh, tmp_path, capsys
    ):
        sh.unit("versitygw-a")
        for name in ("good-bucket", "Bad_Name", ".hidden"):
            (tmp_path / name).mkdir()
        hooks["preflight"](hctx, step, True)
        assert "warning  not valid S3 bucket names, will be hidden: Bad_Name" in capsys.readouterr().out


class TestSmbPassword:
    STEP = {"user": "alice", "still_used_by": ["alice"]}

    def call(self, hooks, hctx, present=True, **step):
        result = hooks["smb_password"](hctx, self.STEP | step, present)
        return result if isinstance(result, tuple) else (result, [])

    def test_before_samba_is_installed_it_is_simply_not_set(self, hooks, hctx, sh):
        sh.replies["pdbedit -V"] = (127, "")
        probe, (action,) = self.call(hooks, hctx)
        assert (probe.ok, probe.have) == (False, "not set")
        assert action.argv == ["smbpasswd", "-a", "alice"]
        assert action.interactive is True  # it asks for the new password
        assert not any("pdbedit -L" in c for c in sh.commands())

    def test_the_password_database_needs_sudo(self, hooks, hctx, sh):
        probe, actions = self.call(hooks, hctx)
        assert (probe.ok, probe.have, actions) == (None, "unknown until sudo", [])
        sh.sudo_probes = True
        sh.replies["pdbedit -L"] = (0, "bob:1001:\nalice:1000:Alice\n")
        hctx._done.clear()
        probe, actions = self.call(hooks, hctx)
        assert (probe.ok, probe.have, actions) == (True, "set", [])
        assert sh.spawned[-1] == ["sudo", "pdbedit", "-L"]

    def test_two_folders_of_one_user_ask_once(self, hooks, hctx, sh):
        sh.replies["pdbedit -V"] = (127, "")
        assert len(self.call(hooks, hctx)[1]) == 1
        probe, actions = self.call(hooks, hctx)
        assert (probe.ok, probe.have, actions) == (True, "as above", [])

    def test_removal_spares_a_user_another_folder_shares_as(self, hooks, hctx, sh):
        sh.sudo_probes = True
        sh.replies["pdbedit -L"] = (0, "alice:1000:Alice\n")
        probe, actions = self.call(hooks, hctx, present=False)
        assert (probe.ok, actions) == (True, [])
        probe, (action,) = self.call(hooks, hctx, present=False, still_used_by=[])
        assert probe.ok is False
        assert action.argv == ["smbpasswd", "-x", "alice"]
        sh.replies["pdbedit -L"] = (0, "")
        assert self.call(hooks, hctx, present=False, still_used_by=[])[0].ok is True


class TestSmbChecks:
    def test_anonymous_listing_must_be_refused(self, hooks, hctx, sh):
        sh.replies["smbclient -N -L //box.example.ts.net"] = (
            1,
            "NT_STATUS_ACCESS_DENIED",
        )
        assert "refused" in hooks["smb_anonymous"](hctx, {"listen": IPS})
        sh.replies["smbclient -N -L //box.example.ts.net"] = (0, "Sharename ...")
        with pytest.raises(Fail, match="anonymous share listing succeeded"):
            hooks["smb_anonymous"](hctx, {"listen": IPS})

    def test_without_magicdns_an_address_is_used(self, hooks, hctx, sh, monkeypatch):
        monkeypatch.setitem(FACTS, "magicdns", lambda _sh: "")
        hctx.values.facts = Facts(sh)
        sh.replies["smbclient"] = (1, "")
        hooks["smb_anonymous"](hctx, {"listen": IPS})
        assert sh.spawned[-1] == ["smbclient", "-N", "-L", "//100.64.0.7"]

    @pytest.fixture
    def share(self, tmp_path):
        """A folder with a file, and a symlink to a host file outside it."""
        root = tmp_path / "share"
        (root / "docs").mkdir(parents=True)
        (root / "docs" / "note.txt").write_text("hello")
        (tmp_path / "host-secret").write_text("not yours")
        (root / "docs" / "leak").symlink_to(tmp_path / "host-secret")
        (root / "docs" / "inside").symlink_to(root / "docs" / "note.txt")
        return root

    def entry(self, root):
        return {"share": "a", "root": str(root), "user": "alice", "listen": IPS}

    def test_no_terminal_no_login_test(self, hooks, hctx, share, sh):
        message = hooks["smb_login"](hctx, self.entry(share))
        assert "login not tested (no terminal" in message
        assert sh.spawned == []

    def test_login_and_symlink_containment(self, hooks, hctx, share, sh, capsys):
        hooks["smb_login"].__globals__["_asked"]["alice"] = "pass word"

        def smbclient(cmd, _stdin):
            if 'get "docs/leak"' in cmd[-1]:
                return 1, "NT_STATUS_OBJECT_NAME_NOT_FOUND opening remote file"
            return 0, "  .  D  0\n  ..  D  0\n"

        sh.replies["smbclient //box.example.ts.net/a -U alice -c"] = smbclient
        message = hooks["smb_login"](hctx, self.entry(share))
        assert message == (
            f"//box.example.ts.net/a: symlink docs/leak -> {share.parent}/host-secret"
            " is not served (OBJECT_NAME_NOT_FOUND)"
        )
        assert [c.rsplit("-c ", 1)[1] for c in sh.commands("smbclient")] == [
            "ls",
            "'get \"docs/note.txt\" /dev/null'",  # control: a plain file downloads
            "'get \"docs/leak\" /dev/null'",
        ]
        assert sh.envs == [{"PASSWD": "pass word"}] * 3  # never in the argv
        assert "pass word" not in " ".join(sh.commands())
        assert "ok       alice can log in and list" in capsys.readouterr().out

    def test_a_served_escape_fails(self, hooks, hctx, share, sh):
        hooks["smb_login"].__globals__["_asked"]["alice"] = "pw"
        sh.replies["smbclient"] = (0, "getting file ... (average 1.0 KiloBytes/sec)")
        with pytest.raises(Fail, match="docs/leak -> .* was served"):
            hooks["smb_login"](hctx, self.entry(share))

    def test_a_failed_login_fails(self, hooks, hctx, share, sh):
        hooks["smb_login"].__globals__["_asked"]["alice"] = "pw"
        sh.replies["smbclient"] = (1, "session setup failed: NT_STATUS_LOGON_FAILURE")
        with pytest.raises(Fail, match="login/listing failed: session setup failed"):
            hooks["smb_login"](hctx, self.entry(share))

    def at_terminal(self, hooks, monkeypatch, *passwords):
        """A terminal whose user types `passwords`; returns the prompts shown."""
        module = hooks["smb_login"].__globals__
        typed, prompts = iter(passwords), []
        monkeypatch.setattr(module["sys"].stdin, "isatty", lambda: True)
        monkeypatch.setattr(
            module["getpass"], "getpass", lambda p: prompts.append(p) or next(typed)
        )
        return prompts

    def test_a_refused_password_is_asked_again(  # noqa: PLR0913
        self, hooks, hctx, share, sh, monkeypatch, capsys
    ):
        prompts = self.at_terminal(hooks, monkeypatch, "typo", "right")
        (share / "docs" / "leak").unlink()
        sh.replies["smbclient"] = lambda _cmd, _stdin: (
            (0, "") if sh.envs[-1] == {"PASSWD": "right"} else (1, "NT_STATUS_LOGON_FAILURE")
        )
        assert "containment not tested" in hooks["smb_login"](hctx, self.entry(share))
        assert len(prompts) == 2
        assert "set with smbpasswd (not the login or sudo password)" in prompts[0]
        assert "not accepted for alice; 2 more attempt(s)" in capsys.readouterr().out
        assert hooks["smb_login"].__globals__["_asked"]["alice"] == "right"

    def test_three_refused_passwords_fail(self, hooks, hctx, share, sh, monkeypatch):
        prompts = self.at_terminal(hooks, monkeypatch, "a", "b", "c")
        sh.replies["smbclient"] = (1, "session setup failed: NT_STATUS_LOGON_FAILURE")
        with pytest.raises(Fail, match="login/listing failed"):
            hooks["smb_login"](hctx, self.entry(share))
        assert len(prompts) == 3
        assert "alice" not in hooks["smb_login"].__globals__["_asked"]

    def test_other_failures_are_not_retried(self, hooks, hctx, share, sh, monkeypatch):
        prompts = self.at_terminal(hooks, monkeypatch, "pw")
        sh.replies["smbclient"] = (1, "NT_STATUS_CONNECTION_REFUSED")
        with pytest.raises(Fail, match="CONNECTION_REFUSED"):
            hooks["smb_login"](hctx, self.entry(share))
        assert len(prompts) == 1

    def test_no_escaping_symlink_nothing_to_test(self, hooks, hctx, share, sh):
        hooks["smb_login"].__globals__["_asked"]["alice"] = "pw"
        (share / "docs" / "leak").unlink()
        sh.replies["smbclient"] = (0, "")
        assert "containment not tested" in hooks["smb_login"](hctx, self.entry(share))


# AWS's published example for S3 ("Signature Calculations for the Authorization
# Header", GET Object and the two GET Bucket requests).
AWS_KEY = ("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
AWS_URL = "https://examplebucket.s3.amazonaws.com"
AWS_NOW = datetime(2013, 5, 24, tzinfo=UTC)


class TestSigV4:
    @pytest.mark.parametrize(
        ("path", "headers", "signed", "signature"),
        [
            (
                "/test.txt",
                {"Range": "bytes=0-9"},
                "host;range;x-amz-content-sha256;x-amz-date",
                "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41",
            ),
            (
                "/?lifecycle",
                None,
                "host;x-amz-content-sha256;x-amz-date",
                "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543",
            ),
            (
                "/?max-keys=2&prefix=J",
                None,
                "host;x-amz-content-sha256;x-amz-date",
                "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7",
            ),
        ],
    )
    def test_aws_test_vectors(self, hooks, path, headers, signed, signature):
        sign = hooks["sigv4_headers"]
        out = sign("GET", AWS_URL + path, *AWS_KEY, "us-east-1", headers, AWS_NOW)
        assert out["Authorization"] == (
            "AWS4-HMAC-SHA256 "
            "Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
            f"SignedHeaders={signed}, Signature={signature}"
        )
        assert out["x-amz-date"] == "20130524T000000Z"
        assert out["x-amz-content-sha256"] == (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )
        assert "host" not in out

    def test_the_query_is_put_in_canonical_order(self, hooks):
        sign = hooks["sigv4_headers"]
        one = sign("GET", AWS_URL + "/?prefix=J&max-keys=2", *AWS_KEY, "x", now=AWS_NOW)
        two = sign("GET", AWS_URL + "/?max-keys=2&prefix=J", *AWS_KEY, "x", now=AWS_NOW)
        assert one == two


BUCKETS = (
    "<ListAllMyBucketsResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>"
    "<Owner><ID>a</ID></Owner><Buckets><Bucket><Name>docs</Name></Bucket>"
    "<Bucket><Name>empty</Name></Bucket></Buckets></ListAllMyBucketsResult>"
)
OBJECTS = (
    "<ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>"
    "<Name>docs</Name><KeyCount>2</KeyCount><IsTruncated>false</IsTruncated>"
    "<Contents><Key>zero</Key><Size>0</Size></Contents>"
    "<Contents><Key>sub dir/a+b é.txt</Key><Size>5</Size></Contents>"
    "</ListBucketResult>"
)
NO_OBJECTS = "<ListBucketResult><KeyCount>0</KeyCount></ListBucketResult>"


@pytest.fixture
def fake_s3(hooks):
    """A local server that checks each request's signature like S3 would."""
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def answer(self, body):
            got = self.headers
            more = {"Range": got["Range"]} if got["Range"] else None
            when = datetime.strptime(got["x-amz-date"], "%Y%m%dT%H%M%SZ")  # noqa: DTZ007
            url = f"http://{got['Host']}{self.path}"
            want = hooks["sigv4_headers"](
                self.command, url, "a", "s3cr3t-s3cr3t", "us-east-1", more, when
            )
            ok = want["Authorization"] == got["Authorization"]
            seen.append((self.command, self.path, got["Range"], ok))
            status = (206 if more else 200) if ok else 403
            if ok and self.command == "HEAD":
                status = 200 if "served" in self.path else 404
            self.send_response(status)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body.encode())

        def do_GET(self):
            if self.path == "/":
                self.answer(BUCKETS)
            elif self.path.startswith("/docs?"):
                self.answer(OBJECTS)
            elif "?" in self.path:
                self.answer(NO_OBJECTS)
            else:
                self.answer("hello")

        def do_HEAD(self):
            self.answer("")

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], seen
    server.shutdown()
    server.server_close()


class TestS3Checks:
    @pytest.fixture
    def entry(self, fake_s3, tmp_path, monkeypatch, hctx, sh):
        monkeypatch.setitem(FACTS, "magicdns", lambda _sh: "")
        hctx.values.facts = Facts(sh)
        env = tmp_path / "s3.env"
        env.write_text(
            "# c\nROOT_ACCESS_KEY_ID=a\nROOT_SECRET_ACCESS_KEY=s3cr3t-s3cr3t\n"
        )
        root = tmp_path / "share"
        (root / "docs").mkdir(parents=True)
        return {
            "credentials": str(env),
            "listen": [],
            "port": fake_s3[0],
            "region": "us-east-1",
            "root": str(root),
            "chroot": "/var/lib/versitygw-a/root",
            "service": "versitygw-a",
            "share": "a",
        }

    def test_smoke_lists_and_reads_with_signed_requests(
        self, hooks, hctx, entry, fake_s3
    ):
        message = hooks["s3_smoke"](hctx, entry)
        assert message == (
            "S3 on 1 endpoints (each listen address + MagicDNS name): "
            "docs (list + read ok), empty (listed)"
        )
        assert fake_s3[1] == [
            ("GET", "/", None, True),
            ("GET", "/docs?list-type=2&max-keys=200", None, True),
            ("GET", "/docs/sub%20dir/a%2Bb%20%C3%A9.txt", "bytes=0-63", True),
            ("GET", "/empty?list-type=2&max-keys=200", None, True),
        ]

    def test_smoke_fails_on_a_refused_request(self, hooks, hctx, entry):
        Path(entry["credentials"]).write_text("ROOT_SECRET_ACCESS_KEY=wrong-wrong\n")
        with pytest.raises(Fail, match="listing buckets -> HTTP 403"):
            hooks["s3_smoke"](hctx, entry)

    def test_credentials_only_root_can_read_need_sudo(self, hooks, hctx, entry):
        Path(entry["credentials"]).chmod(0)
        with pytest.raises(Fail, match="cannot be read without sudo"):
            hooks["s3_smoke"](hctx, entry)

    def test_the_secret_never_reaches_the_log(self, hooks, hctx, entry, tmp_path):
        hooks["s3_smoke"](hctx, entry)
        assert hctx.log.scrub("x s3cr3t-s3cr3t") == "x <redacted>"

    MOUNTS = (
        "805 620 259:2 {chroot} / rw,relatime - ext4 /dev/nvme0n1p2 rw\n"
        "806 805 0:5 / /dev rw,nosuid - devtmpfs devtmpfs rw\n"
        "807 805 0:22 / /proc rw - proc proc rw\n"
        "808 805 259:2 /usr/bin/versitygw /usr/bin/versitygw ro - ext4 /dev/x rw\n"
        "809 805 259:5 {root} {root} rw - ext4 /dev/y rw\n"
    )

    def sandbox(self, entry, tmp_path, sh, extra=""):
        """Point the service's MainPID at a mount table of our making."""
        table = self.MOUNTS.format(chroot=entry["chroot"], root=entry["root"]) + extra
        (tmp_path / "proc").mkdir(exist_ok=True)
        (tmp_path / "proc" / "mountinfo").write_text(table)
        sh.replies["systemctl show versitygw-a -p MainPID"] = (0, f"..{tmp_path}/proc")

    def test_host_mounts_reads_a_mount_table(self, hooks, entry):
        table = self.MOUNTS.format(chroot="/var/lib/x/root", root="/srv/my\\040share")
        assert hooks["host_mounts"](table) == {
            "/": "/var/lib/x/root",
            "/usr/bin/versitygw": "/usr/bin/versitygw",
            "/srv/my share": "/srv/my share",
        }

    def test_sandbox_with_no_symlink_to_try(self, hooks, hctx, entry, tmp_path, sh):
        self.sandbox(entry, tmp_path, sh)
        message = hooks["s3_sandbox"](hctx, entry)
        assert message == (
            f"sandbox: only {entry['root']} and /usr/bin/versitygw from the host "
            "are visible to versitygw-a; no escaping symlink found to test"
        )

    def test_sandbox_asks_for_an_escaping_symlink(
        self, hooks, hctx, entry, tmp_path, sh, fake_s3
    ):
        self.sandbox(entry, tmp_path, sh)
        (tmp_path / "host-secret").write_text("not yours")
        root = Path(entry["root"])
        (root / "top-level").symlink_to(tmp_path / "host-secret")  # not an object
        (root / "docs" / "leak").symlink_to(tmp_path / "host-secret")
        message = hooks["s3_sandbox"](hctx, entry)
        assert message.endswith(
            f"symlink s3://docs/leak -> {tmp_path}/host-secret is not served (404)"
        )
        assert fake_s3[1] == [("HEAD", "/docs/leak", None, True)]

    def test_sandbox_fails_when_the_symlink_is_served(
        self, hooks, hctx, entry, tmp_path, sh
    ):
        self.sandbox(entry, tmp_path, sh)
        (tmp_path / "host-secret").write_text("not yours")
        (Path(entry["root"]) / "docs" / "served").symlink_to(tmp_path / "host-secret")
        with pytest.raises(Fail, match="s3://docs/served served host file"):
            hooks["s3_sandbox"](hctx, entry)

    def test_sandbox_fails_on_any_other_host_mount(
        self, hooks, hctx, entry, tmp_path, sh
    ):
        leak = "810 805 259:2 /etc /etc ro - ext4 /dev/nvme0n1p2 rw\n"
        self.sandbox(entry, tmp_path, sh, extra=leak)
        with pytest.raises(Fail, match="sandbox not in effect: versitygw-a mounts"):
            hooks["s3_sandbox"](hctx, entry)

    def test_sandbox_fails_when_the_root_is_the_host_root(
        self, hooks, hctx, entry, tmp_path, sh
    ):
        self.sandbox(entry | {"chroot": "/"}, tmp_path, sh)
        with pytest.raises(Fail, match="sandbox not in effect"):
            hooks["s3_sandbox"](hctx, entry)

    def test_client_help_is_one_usable_line(self, hooks, hctx, entry):
        (Path(entry["root"]) / "Not-A-Bucket").mkdir()
        message = hooks["s3_client_help"](hctx, entry | {"listen": ["fd7a::7"]})
        assert message == (
            "a over S3 (path-style; buckets: docs):\n"
            f"AWS_ENDPOINT_URL=http://[fd7a::7]:{entry['port']} AWS_REGION=us-east-1 "
            "AWS_ACCESS_KEY_ID=a AWS_SECRET_ACCESS_KEY=s3cr3t-s3cr3t"
        )


# ---------------------------------------------------------------- packaging


class TestSingleFile:
    def test_the_built_file_carries_the_recipe(self, tmp_path, sandbox, capsys):
        out = tmp_path / "sux"
        build_single_file.main(out_path=out)
        capsys.readouterr()
        env = os.environ | {
            "HOME": str(sandbox / "home"),
            "XDG_CONFIG_HOME": str(sandbox / "config"),
            "XDG_STATE_HOME": str(sandbox / "state"),
        }

        def run(*args):
            argv = [sys.executable, str(out), "install", *args]
            return subprocess.run(
                argv,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )

        listing = run()
        assert listing.returncode == 0, listing.stderr
        assert re.search(r"network-folder\s+not set up\s+Share folders", listing.stdout)
        asked = run("network-folder")  # no terminal: the questions, and no change
        assert asked.returncode == 1, asked.stderr
        assert "network-folder has questions that are not answered yet:" in asked.stdout
        assert "share      Share name" in asked.stdout
        assert not (sandbox / "config" / "sux").exists()
