import hashlib
import json
import os
import tarfile
import time

import pytest

from sux.install import resources
from sux.install.log import Fail
from sux.install.model import (
    KINDS,
    Action,
    Ctx,
    Probe,
    Resource,
    check_converged,
    probe_and_plan,
    register,
)
from sux.install.resources import (
    SECRET_MARK,
    AptRes,
    AptSourceRes,
    DaemonReloadRes,
    DebRes,
    DirRes,
    DownloadRes,
    FileRes,
    GroupMemberRes,
    JsonMergeRes,
    PythonRes,
    SecretRes,
    UnitRes,
    fetch,
    json_merge,
    json_unmerge,
)

pytestmark = pytest.mark.usefixtures("sandbox")


def plan(res, ctx):
    """Probe, and plan when a change is needed (as probe_and_plan does)."""
    p = res.probe(ctx)
    actions = res.plan(ctx, p) if p.ok is False else []
    if actions:
        ctx.changing.add(res.key)
    return p, actions


def shown(actions, sh):
    return [a.shown(sh) for a in actions]


def apply(actions, sh):
    for a in actions:
        a.execute(sh)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def test_registry_has_every_step_kind():
    assert set(KINDS) == {
        "apt",
        "apt_source",
        "deb",
        "download",
        "file",
        "dir",
        "secret",
        "unit",
        "group_member",
        "json_merge",
        "libvirt_domain",
            "libvirt_network",
            "libvirt_dhcp_host",
            "image",
            "python",
    }


def test_adding_a_kind_is_one_class(ctx, monkeypatch):
    monkeypatch.setattr("sux.install.model.KINDS", dict(KINDS))

    @register
    class Hello(Resource):
        kind = "hello"

        def probe(self, ctx):  # noqa: ARG002
            return Probe(False, "silent")

        def plan(self, ctx, p):  # noqa: ARG002
            return [Action("say hello", ["echo", "hello"], sudo=False)]

    from sux.install import model  # noqa: PLC0415

    assert model.KINDS["hello"] is Hello
    assert Hello({}).key == "hello:"


class TestFile:
    def step(self, tmp_path, me, **extra):
        return {"path": str(tmp_path / "etc" / "app.conf"), "content": "a\nb\n"} | me | extra

    def test_missing_plans_a_write_with_diff(self, tmp_path, me, ctx, sh):
        res = FileRes(self.step(tmp_path, me))
        p, actions = plan(res, ctx)
        assert p.ok is False
        assert p.have == "missing"
        assert len(actions) == 1
        assert actions[0].stdin == "a\nb\n"
        assert "+a" in actions[0].detail
        assert shown(actions, sh)[0].startswith("sudo install -D -m 0644 -o")
        apply(actions, sh)
        assert res.probe(ctx).ok is True

    def test_content_differs_backs_up_then_writes(self, tmp_path, me, ctx, sh):
        res = FileRes(self.step(tmp_path, me))
        res.path.parent.mkdir()
        res.path.write_text("old\n")
        res.path.chmod(0o644)
        p, actions = plan(res, ctx)
        assert p.ok is False
        assert p.reason == "content differ"
        lines = shown(actions, sh)
        assert lines[0] == f"sudo install -d -m 0700 {ctx.backup_dir}"
        assert lines[1] == f"sudo cp -a --parents {res.path} {ctx.backup_dir}"
        assert str(ctx.backup_dir).endswith("backups/demo/STAMP")
        assert "-old" in actions[2].detail
        assert "+a" in actions[2].detail

    def test_backup_dir_is_created_once_per_run(self, tmp_path, me, ctx, sh):
        first = FileRes(self.step(tmp_path, me))
        second = FileRes(self.step(tmp_path, me, path=str(tmp_path / "etc" / "two")))
        first.path.parent.mkdir()
        first.path.write_text("old\n")
        second.path.write_text("old\n")
        lines = shown(plan(first, ctx)[1] + plan(second, ctx)[1], sh)
        assert sum("install -d -m 0700" in ln for ln in lines) == 1
        assert sum(" cp -a --parents " in ln for ln in lines) == 2

    def test_mode_is_compared_as_well_as_content(self, tmp_path, me, ctx, sh):
        res = FileRes(self.step(tmp_path, me, mode="0600"))
        res.path.parent.mkdir()
        res.path.write_text("a\nb\n")
        res.path.chmod(0o644)
        p, actions = plan(res, ctx)
        assert p.ok is False
        assert p.reason == "owner/mode differ"
        assert shown(actions, sh)[1] == f"sudo chmod 0600 {res.path}"
        apply(actions, sh)
        assert res.probe(ctx).ok is True

    def test_owner_is_compared(self, tmp_path, me, ctx):
        res = FileRes(self.step(tmp_path, me, owner="root"))
        res.path.parent.mkdir()
        res.path.write_text("a\nb\n")
        res.path.chmod(0o644)
        p, actions = plan(res, ctx)
        assert p.ok is False
        assert actions[0].argv[:2] == ["chown", f"root:{me['group']}"]

    def test_mode_accepts_toml_octal_int(self, tmp_path, me):
        assert FileRes(self.step(tmp_path, me, mode=0o640)).mode == 0o640

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can read everything")
    def test_unreadable_is_unknown_until_sudo(self, tmp_path, me, ctx, sh):
        res = FileRes(self.step(tmp_path, me, mode="0000"))
        res.path.parent.mkdir()
        res.path.write_text("a\nb\n")
        res.path.chmod(0)
        p, actions = plan(res, ctx)
        assert p.ok is None
        assert "unknown until sudo" in p.have
        assert actions == []
        assert sh.spawned == []

        sh.sudo_probes = True
        sh.replies["cat"] = (0, "a\nb\n")
        assert res.probe(ctx).ok is True
        assert sh.spawned == [["sudo", "cat", str(res.path)]]
        sh.replies["cat"] = (0, "tampered\n")
        p, actions = plan(res, ctx)
        assert p.ok is False
        assert actions[-1].stdin == "a\nb\n"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can read everything")
    def test_unsearchable_dir_is_unknown_until_sudo(self, tmp_path, me, ctx, sh):
        res = FileRes(self.step(tmp_path, me))
        res.path.parent.mkdir()
        res.path.parent.chmod(0)
        try:
            assert res.probe(ctx).ok is None
            sh.sudo_probes = True
            sh.replies["stat"] = (1, "")
            p, actions = plan(res, ctx)
            assert p.have == "missing"
            assert len(actions) == 1
        finally:
            res.path.parent.chmod(0o755)

    def test_absent(self, tmp_path, me, ctx, sh):
        res = FileRes(self.step(tmp_path, me), present=False)
        assert res.probe(ctx) == Probe(True, "absent")
        res.path.parent.mkdir()
        res.path.write_text("x")
        p, actions = plan(res, ctx)
        assert p.ok is False
        assert shown(actions, sh)[-2:] == [
            f"sudo cp -a --parents {res.path} {ctx.backup_dir}",
            f"sudo rm -f {res.path}",
        ]


class TestDir:
    def test_states(self, tmp_path, me, ctx, sh):
        res = DirRes({"path": str(tmp_path / "data"), "mode": "0750"} | me)
        p, actions = plan(res, ctx)
        assert p.ok is False
        assert shown(actions, sh) == [
            f"sudo install -d -m 0750 -o {me['owner']} -g {me['group']} {res.path}"
        ]
        apply(actions, sh)
        assert res.probe(ctx).ok is True
        res.path.chmod(0o700)
        assert res.probe(ctx).ok is False

    def test_absent_removes_only_when_empty(self, tmp_path, me, ctx, sh):
        res = DirRes({"path": str(tmp_path / "data")} | me, present=False)
        assert res.probe(ctx).ok is True
        res.path.mkdir()
        p, actions = plan(res, ctx)
        assert p.ok is False
        assert shown(actions, sh) == [f"sudo rmdir {res.path}"]
        (res.path / "keep").write_text("data")
        p = res.probe(ctx)
        assert p.ok is True
        assert "kept" in p.have
        ctx.changing.add(f"file:{res.path / 'keep'}")  # removed in this run
        assert res.probe(ctx).ok is False


class TestSecret:
    def step(self, tmp_path, me):
        return {
            "path": str(tmp_path / "app.env"),
            "content": f"KEY_ID=shared\nKEY_SECRET={SECRET_MARK}\n",
        } | me

    def test_generated_once_then_kept(self, tmp_path, me, ctx, sh):
        res = SecretRes(self.step(tmp_path, me))
        p, actions = plan(res, ctx)
        assert p.ok is False
        assert len(actions) == 1
        assert actions[0].secret
        assert "new random secret" in actions[0].desc
        assert shown(actions, sh)[0].endswith("< secret")
        apply(actions, sh)
        first = res.path.read_text()
        assert first.startswith("KEY_ID=shared\nKEY_SECRET=")
        assert res.path.stat().st_mode & 0o777 == 0o600
        assert res.probe(ctx).ok is True
        assert plan(res, ctx)[1] == []  # never regenerated
        assert res.path.read_text() == first

    def test_existing_secret_survives_a_content_change(self, tmp_path, me, ctx, sh):
        res = SecretRes(self.step(tmp_path, me))
        res.path.write_text("KEY_ID=old\nKEY_SECRET=keepme-keepme\n")
        res.path.chmod(0o600)
        p, actions = plan(res, ctx)
        assert p.ok is False
        assert "keeps the existing secret" in actions[-1].desc
        assert actions[-1].stdin == "KEY_ID=shared\nKEY_SECRET=keepme-keepme\n"
        assert "cp -a --parents" in shown(actions, sh)[-2]

    def test_secret_is_scrubbed_from_the_log(self, tmp_path, me, ctx, sh):
        res = SecretRes(self.step(tmp_path, me))
        res.path.write_text("KEY_ID=shared\nKEY_SECRET=keepme-keepme\n")
        res.path.chmod(0o600)
        assert res.probe(ctx).ok is True
        assert sh.log.scrub("x keepme-keepme y") == "x <redacted> y"

    def test_default_content_is_just_the_secret(self, tmp_path, me, ctx, sh):
        res = SecretRes({"path": str(tmp_path / "token"), "bytes": 8} | me)
        apply(plan(res, ctx)[1], sh)
        assert len(res.path.read_text().strip()) >= 8
        assert res.probe(ctx).ok is True

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can read everything")
    def test_unreadable_is_unknown_until_sudo(self, tmp_path, me, ctx, sh):
        step = self.step(tmp_path, me) | {"mode": "0000"}
        res = SecretRes(step)
        res.path.write_text("KEY_ID=shared\nKEY_SECRET=keepme-keepme\n")
        res.path.chmod(0)
        p, actions = plan(res, ctx)
        assert p.ok is None
        assert actions == []
        sh.sudo_probes = True
        sh.replies["cat"] = (0, "KEY_ID=shared\nKEY_SECRET=keepme-keepme\n")
        assert res.probe(ctx).ok is True

    def test_content_needs_the_placeholder(self, tmp_path, me):
        with pytest.raises(Fail, match=r"content must use \$secret"):
            SecretRes({"path": str(tmp_path / "x"), "content": "no secret"} | me)


class TestJsonMerge:
    DATA = {"runtimes": {"kata": {"path": "/opt/kata/bin/shim"}}}

    def step(self, tmp_path, me):
        return {"path": str(tmp_path / "daemon.json"), "data": self.DATA} | me

    def test_merge_keeps_other_keys(self, tmp_path, me, ctx, sh):
        res = JsonMergeRes(self.step(tmp_path, me))
        res.path.write_text('{"runtimes": {"nvidia": {"path": "nv"}}, "debug": true}')
        p, actions = plan(res, ctx)
        assert p.ok is False
        assert "cp -a --parents" in shown(actions, sh)[-2]
        apply(actions, sh)
        assert json.loads(res.path.read_text()) == {
            "runtimes": {"nvidia": {"path": "nv"}, "kata": self.DATA["runtimes"]["kata"]},
            "debug": True,
        }
        assert res.probe(ctx).ok is True

    def test_missing_file_is_created(self, tmp_path, me, ctx, sh):
        res = JsonMergeRes(self.step(tmp_path, me))
        p, actions = plan(res, ctx)
        assert p.ok is False
        assert len(actions) == 1
        apply(actions, sh)
        assert json.loads(res.path.read_text()) == self.DATA

    def test_absent_removes_only_our_keys(self, tmp_path, me, ctx, sh):
        res = JsonMergeRes(self.step(tmp_path, me), present=False)
        assert res.probe(ctx).ok is True  # no file
        res.path.write_text(json.dumps(json_merge({"debug": True}, self.DATA)))
        p, actions = plan(res, ctx)
        assert p.ok is False
        apply(actions, sh)
        assert json.loads(res.path.read_text()) == {"debug": True}

    def test_unmerge_leaves_changed_values(self):
        have = {"a": {"b": 1, "c": 2}, "d": 3}
        assert json_unmerge(have, {"a": {"b": 1}, "d": 4}) == {"a": {"c": 2}, "d": 3}

    def test_invalid_json_fails(self, tmp_path, me, ctx):
        res = JsonMergeRes(self.step(tmp_path, me))
        res.path.write_text("{not json")
        with pytest.raises(Fail, match="not valid JSON"):
            res.probe(ctx)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can read everything")
    def test_unreadable_is_unknown_until_sudo(self, tmp_path, me, ctx):
        res = JsonMergeRes(self.step(tmp_path, me))
        res.path.write_text("{}")
        res.path.chmod(0)
        assert res.probe(ctx).ok is None


class TestApt:
    QUERY = "dpkg-query -W"

    def test_installed_is_ok(self, ctx, sh):
        sh.replies[self.QUERY] = (0, "samba installed\nsmbclient installed\n")
        assert AptRes({"packages": ["samba", "smbclient"]}).probe(ctx).ok is True

    def test_missing_plans_install_of_only_what_is_missing(self, ctx, sh):
        sh.replies[self.QUERY] = (1, "samba installed\nsmbclient not-installed\n")
        p, actions = plan(AptRes({"packages": ["samba", "smbclient"]}), ctx)
        assert p == Probe(False, "missing: smbclient")
        assert len(actions) == 1
        assert actions[0].argv[:6] == [
            "env",
            "DEBIAN_FRONTEND=noninteractive",
            "UCF_FORCE_CONFFOLD=1",
            "apt-get",
            "install",
            "-y",
        ]
        assert "--no-install-recommends" in actions[0].argv
        assert actions[0].argv[-1] == "smbclient"

    def test_stale_lists_plan_one_update_first(self, ctx, sh, sandbox):
        old = time.time() - 3 * 24 * 3600
        os.utime(sandbox / "apt-stamp", (old, old))
        sh.replies[self.QUERY] = (1, "")
        first = plan(AptRes({"packages": ["a"]}), ctx)[1]
        second = plan(AptRes({"packages": ["b"]}), ctx)[1]
        assert shown(first, sh)[0] == "sudo apt-get update"
        assert len(first) == 2
        assert len(second) == 1

    def test_new_apt_source_plans_an_update(self, ctx, sh):
        sh.replies[self.QUERY] = (1, "")
        ctx.changing.add("apt_source:vendor")
        actions = plan(AptRes({"packages": ["a"]}), ctx)[1]
        assert shown(actions, sh)[0] == "sudo apt-get update"

    def test_no_autostart_guards_and_always_cleans_up(self, ctx, sh, sandbox):
        sh.replies[self.QUERY] = (1, "")
        guard = sandbox / "policy-rc.d"
        seen = []

        def apt(cmd, stdin):  # noqa: ARG001
            seen.append(guard.read_text())
            return 100, ""

        sh.replies["env DEBIAN_FRONTEND"] = apt
        actions = plan(AptRes({"packages": ["samba"], "no_autostart": True}), ctx)[1]
        assert len(actions) == 1
        assert "services blocked" in actions[0].desc
        with pytest.raises(Fail, match="command failed"):
            actions[0].execute(sh)
        assert seen == ["#!/bin/sh\nexit 101\n"]  # in place while apt ran
        assert not guard.exists()  # removed although the install failed

    def test_stale_guard_is_detected_and_removed(self, ctx, sh, sandbox):
        sh.replies[self.QUERY] = (0, "samba installed\n")
        (sandbox / "policy-rc.d").write_text("#!/bin/sh\nexit 101\n")
        res = AptRes({"packages": ["samba"], "no_autostart": True})
        p, actions = plan(res, ctx)
        assert p.ok is False
        assert "interrupted" in p.reason
        assert shown(actions, sh) == [f"sudo rm -f {sandbox / 'policy-rc.d'}"]
        apply(actions, sh)
        assert res.probe(ctx).ok is True

    def test_foreign_guard_is_not_touched(self, ctx, sh, sandbox):
        sh.replies[self.QUERY] = (1, "")
        (sandbox / "policy-rc.d").write_text("#!/bin/sh\nexit 0\n")
        res = AptRes({"packages": ["samba"], "no_autostart": True})
        with pytest.raises(Fail, match="not ours"):
            plan(res, ctx)

    def test_absent(self, ctx, sh):
        sh.replies[self.QUERY] = (1, "samba installed\n")
        res = AptRes({"packages": ["samba", "smbclient"]}, present=False)
        p, actions = plan(res, ctx)
        assert p.ok is False
        assert shown(actions, sh) == ["sudo apt-get remove -y samba"]
        sh.replies[self.QUERY] = (1, "")
        assert res.probe(ctx).ok is True


class TestAptSource:
    KEY = b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n"

    def step(self, tmp_path):
        key = tmp_path / "vendor.asc"
        key.write_bytes(self.KEY)
        return {
            "name": "vendor",
            "uris": "https://apt.example.com/debian",
            "suites": ["noble"],
            "architectures": ["amd64"],
            "key_url": key.as_uri(),
            "key_sha256": sha(self.KEY),
        }

    def test_missing_plans_keyring_and_deb822_source(self, tmp_path, ctx, sh, sandbox):
        res = AptSourceRes(self.step(tmp_path))
        p, actions = plan(res, ctx)
        assert p == Probe(False, "missing")
        assert [a.desc.split()[0] for a in actions] == ["download", "install", "write"]
        assert actions[2].stdin == (
            "# Managed by sux install.\n"
            "Types: deb\n"
            "URIs: https://apt.example.com/debian\n"
            "Suites: noble\n"
            "Components: main\n"
            "Architectures: amd64\n"
            f"Signed-By: {sandbox / 'keyrings' / 'vendor.asc'}\n"
        )
        apply(actions, sh)
        assert (sandbox / "keyrings" / "vendor.asc").read_bytes() == self.KEY
        assert res.path == sandbox / "sources" / "vendor.sources"
        assert res.probe(ctx) == Probe(True, "enabled")

    def test_disabled_by_release_upgrade_is_reenabled(self, tmp_path, ctx, sh, monkeypatch):
        res = AptSourceRes(self.step(tmp_path))
        apply(plan(res, ctx)[1], sh)
        res.path.write_text(res.content + "Enabled: no\n")
        asked = []
        monkeypatch.setattr(resources, "url_exists", lambda url: asked.append(url) or True)
        p, actions = plan(res, ctx)
        assert p == Probe(False, "disabled (release upgrade?)")
        assert asked == ["https://apt.example.com/debian/dists/noble/InRelease"]
        assert [a.desc.split()[0] for a in actions] == ["create", "back", "write"]
        assert "-Enabled: no" in actions[-1].detail

    @pytest.mark.parametrize(
        ("found", "why"), [(False, "not published yet"), (None, "could not be reached")]
    )
    def test_left_disabled_until_the_vendor_publishes(
        self, tmp_path, ctx, sh, monkeypatch, found, why
    ):
        res = AptSourceRes(self.step(tmp_path))
        apply(plan(res, ctx)[1], sh)
        res.path.write_text(res.content + "Enabled: no\n")
        monkeypatch.setattr(resources, "url_exists", lambda _url: found)
        p, actions = plan(res, ctx)
        assert p.ok is True
        assert why in p.have
        assert actions == []

    def test_absent(self, tmp_path, ctx, sh):
        res = AptSourceRes(self.step(tmp_path))
        apply(plan(res, ctx)[1], sh)
        gone = AptSourceRes(self.step(tmp_path), present=False)
        p, actions = plan(gone, ctx)
        assert p.ok is False
        apply(actions, sh)
        assert not res.path.exists()
        assert not res.keyring.exists()
        assert gone.probe(ctx).ok is True


class TestDeb:
    def step(self, tmp_path):
        deb = tmp_path / "tool_1.2.0_amd64.deb"
        deb.write_bytes(b"deb")
        return {
            "name": "tool",
            "url": deb.as_uri(),
            "sha256": sha(b"deb"),
            "version": "1.2.0",
        }

    def test_states(self, tmp_path, ctx, sh):
        res = DebRes(self.step(tmp_path))
        sh.replies["dpkg-query"] = (0, "1.2.0-1\tinstalled")
        assert res.probe(ctx).ok is True
        sh.replies["dpkg-query"] = (0, "1.1.0\tinstalled")
        assert res.probe(ctx) == Probe(False, "installed 1.1.0")
        sh.replies["dpkg-query"] = (1, "")
        p, actions = plan(res, ctx)
        assert p == Probe(False, "not installed")
        cached = ctx.cache_dir / "tool_1.2.0_amd64.deb"
        assert shown(actions, sh)[1] == f"sudo dpkg -i {cached}"
        actions[0].execute(sh)  # the download, into the cache
        assert cached.read_bytes() == b"deb"

    def test_absent(self, tmp_path, ctx, sh):
        res = DebRes(self.step(tmp_path), present=False)
        sh.replies["dpkg-query"] = (0, "1.2.0\tinstalled")
        p, actions = plan(res, ctx)
        assert p.ok is False
        assert shown(actions, sh) == ["sudo dpkg -r tool"]


class TestFetch:
    def test_downloads_and_verifies(self, tmp_path, ctx):
        src = tmp_path / "src.bin"
        src.write_bytes(b"payload")
        dest = tmp_path / "cache" / "src.bin"
        fetch(ctx, src.as_uri(), dest, sha(b"payload"))
        assert dest.read_bytes() == b"payload"
        src.unlink()
        fetch(ctx, src.as_uri(), dest, sha(b"payload"))  # cached: no download

    def test_part_file_is_removed_on_hash_mismatch(self, tmp_path, ctx):
        src = tmp_path / "src.bin"
        src.write_bytes(b"payload")
        dest = tmp_path / "cache" / "src.bin"
        with pytest.raises(Fail, match="sha256 mismatch"):
            fetch(ctx, src.as_uri(), dest, "0" * 64)
        assert list(dest.parent.iterdir()) == []

    def test_part_file_is_removed_on_any_failure(self, tmp_path, ctx, monkeypatch):
        src = tmp_path / "src.bin"
        src.write_bytes(b"payload")
        dest = tmp_path / "cache" / "src.bin"

        def interrupted(resp, out):  # noqa: ARG001
            out.write(b"pay")
            raise KeyboardInterrupt

        monkeypatch.setattr(resources.shutil, "copyfileobj", interrupted)
        with pytest.raises(KeyboardInterrupt):
            fetch(ctx, src.as_uri(), dest, sha(b"payload"))
        assert list(dest.parent.iterdir()) == []

    def test_unreachable_is_a_clean_failure(self, tmp_path, ctx):
        with pytest.raises(Fail, match="download failed"):
            fetch(ctx, (tmp_path / "nope").as_uri(), tmp_path / "cache" / "x", "0" * 64)


class TestDownload:
    def test_file(self, tmp_path, me, ctx, sh):
        src = tmp_path / "tool"
        src.write_bytes(b"binary")
        step = {
            "path": str(tmp_path / "opt" / "tool" / "1.0" / "tool"),
            "url": src.as_uri(),
            "sha256": sha(b"binary"),
            "mode": "0755",
            "link": str(tmp_path / "opt" / "tool" / "current"),
        } | me
        res = DownloadRes(step)
        p, actions = plan(res, ctx)
        assert p == Probe(False, "missing")
        assert [a.desc.split()[0] for a in actions] == ["download", "install", "link"]
        actions[0].execute(sh)
        actions[1].execute(sh)
        assert res.path.read_bytes() == b"binary"
        p = res.probe(ctx)
        assert p.ok is False
        assert "does not point to it" in p.reason
        os.symlink(res.path, step["link"])
        assert res.probe(ctx).ok is True

        res.path.write_bytes(b"tampered")
        assert res.probe(ctx) == Probe(False, "other content")

    def test_extract_is_tracked_by_a_marker(self, tmp_path, me, ctx, sh):
        archive = tmp_path / "tool.tar"
        with tarfile.open(archive, "w") as tar:
            tar.add(__file__, arcname="tool/readme")
        digest = sha(archive.read_bytes())
        step = {
            "path": str(tmp_path / "opt" / "tool" / "1.0"),
            "url": archive.as_uri(),
            "sha256": digest,
            "extract": True,
            "strip": 1,
        } | me
        res = DownloadRes(step)
        p, actions = plan(res, ctx)
        assert p.ok is False
        lines = shown(actions, sh)
        assert lines[1] == f"sudo install -d -m 0755 {res.path}"
        assert lines[2].startswith(f"sudo tar -xf {ctx.cache_dir / 'tool.tar'} -C")
        assert lines[2].endswith("--strip-components=1")
        apply(actions, sh)  # tar itself is not emulated; the marker is written
        assert (res.path / ".sux-sha256").read_text() == digest + "\n"
        assert res.probe(ctx).ok is True

        gone = DownloadRes(step, present=False)
        p, actions = plan(gone, ctx)
        assert shown(actions, sh) == [f"sudo rm -rf {res.path}"]
        (res.path / ".sux-sha256").unlink()
        assert gone.probe(ctx) == Probe(True, "not this download, kept")


class TestUnit:
    def test_running_and_current_is_ok(self, tmp_path, ctx, sh):
        conf = tmp_path / "app.conf"
        conf.write_text("x")
        sh.unit("app.service", since=time.time() + 5)
        res = UnitRes({"name": "app.service", "restart_on": [str(conf)]})
        assert res.probe(ctx) == Probe(True, "enabled, active")

    def test_not_installed_plans_enable_and_start(self, ctx, sh):
        res = UnitRes({"name": "app.service"})
        p, actions = plan(res, ctx)
        assert p == Probe(False, "not-found, inactive")
        assert shown(actions, sh) == [
            "sudo systemctl enable app.service",
            "sudo systemctl restart app.service",
        ]
        apply(actions, sh)
        assert res.probe(ctx).ok is True

    def test_restarts_when_an_input_changed_after_start(self, tmp_path, ctx, sh):
        conf = tmp_path / "app.conf"
        conf.write_text("x")
        sh.unit("app.service", since=time.time() - 60)
        step = {
            "name": "app.service",
            "restart_on": [str(conf)],
            "validate": ["testparm", "-s", str(conf)],
        }
        p, actions = plan(UnitRes(step), ctx)
        assert p.ok is False
        assert p.reason == "changed after start: app.conf"
        assert shown(actions, sh) == [
            f"testparm -s {conf}",  # validated first, and not as root
            "sudo systemctl restart app.service",
        ]

    def test_failed_validation_stops_before_the_restart(self, ctx, sh):
        sh.replies["testparm"] = (1, "bad config")
        actions = plan(UnitRes({"name": "a.service", "validate": ["testparm"]}), ctx)[1]
        with pytest.raises(Fail, match="command failed"):
            apply(actions, sh)
        assert sh.commands("systemctl restart") == []

    def test_restarts_when_an_input_changes_in_this_run(self, tmp_path, ctx, sh):
        sh.unit("app.service", since=time.time() + 5)
        step = {"name": "app.service", "restart_on": ["/etc/app.conf", "deb:tool"]}
        assert UnitRes(step).probe(ctx).ok is True
        ctx.changing.update({"file:/etc/app.conf", "deb:tool", "file:/etc/other"})
        p = UnitRes(step).probe(ctx)
        assert p.ok is False
        assert p.reason == "changes in this run: deb:tool, file:/etc/app.conf"

    def test_stopped(self, ctx, sh):
        sh.unit("app.timer")
        res = UnitRes({"name": "app.timer", "state": "stopped"})
        p, actions = plan(res, ctx)
        assert p.ok is False
        assert shown(actions, sh) == ["sudo systemctl disable --now app.timer"]
        apply(actions, sh)
        assert res.probe(ctx).ok is True

    def test_masked(self, ctx, sh):
        res = UnitRes({"name": "nmbd", "state": "masked"})
        p, actions = plan(res, ctx)
        assert p.ok is False
        assert shown(actions, sh) == ["sudo systemctl mask --now nmbd"]
        apply(actions, sh)
        assert res.probe(ctx).ok is True
        undo = UnitRes({"name": "nmbd", "state": "masked"}, present=False)
        assert shown(plan(undo, ctx)[1], sh) == ["sudo systemctl unmask nmbd"]

    def test_on_demand_restarts_only_if_running_stale(self, tmp_path, ctx, sh):
        conf = tmp_path / "proxy.service"
        conf.write_text("x")
        step = {"name": "proxy.service", "state": "on-demand", "restart_on": [str(conf)]}
        assert UnitRes(step).probe(ctx).ok is True  # not running: fine
        sh.unit("proxy.service", enabled="static", since=time.time() - 60)
        p, actions = plan(UnitRes(step), ctx)
        assert p.ok is False
        assert shown(actions, sh) == ["sudo systemctl restart proxy.service"]

    def test_absent(self, ctx, sh):
        res = UnitRes({"name": "app.service"}, present=False)
        assert res.probe(ctx).ok is True  # never installed
        sh.unit("app.service")
        p, actions = plan(res, ctx)
        assert p.ok is False
        assert shown(actions, sh) == ["sudo systemctl disable --now app.service"]

    def test_unknown_state_is_rejected(self):
        with pytest.raises(Fail, match="state must be one of"):
            UnitRes({"name": "x", "state": "sideways"})


class TestDaemonReload:
    def test_needed_once_when_unit_files_change(self, ctx, sh, sandbox):
        first = DaemonReloadRes({"units": ["app.service"]})
        last = DaemonReloadRes({"units": ["app.service"], "label": "last"})
        assert first.probe(ctx).ok is True
        ctx.changing.add(f"file:{sandbox / 'units' / 'app.service'}")
        ctx.changing.add("file:/etc/app.conf")
        p, actions = plan(first, ctx)
        assert p == Probe(False, "unit files change in this run")
        assert shown(actions, sh) == ["sudo systemctl daemon-reload"]
        assert last.probe(ctx).ok is True  # already covered by the first

    def test_needed_when_systemd_says_so(self, ctx, sh):
        sh.need_reload = True
        res = DaemonReloadRes({"units": ["app.service"]})
        assert res.probe(ctx) == Probe(False, "reload needed")
        assert DaemonReloadRes({"units": ["app.service"]}).probe(ctx).ok is True


class TestGroupMember:
    def test_states(self, ctx, sh):
        sh.replies["id -nG"] = (0, "mike adm\n")
        res = GroupMemberRes({"user": "mike", "group": "kvm"})
        p, actions = plan(res, ctx)
        assert p == Probe(False, "not a member")
        assert shown(actions, sh) == ["sudo gpasswd -a mike kvm"]
        sh.replies["id -nG"] = (0, "mike adm kvm\n")
        assert res.probe(ctx).ok is True
        gone = GroupMemberRes({"user": "mike", "group": "kvm"}, present=False)
        assert shown(plan(gone, ctx)[1], sh) == ["sudo gpasswd -d mike kvm"]

    def test_unknown_user_fails(self, ctx, sh):
        sh.replies["id -nG"] = (1, "")
        with pytest.raises(Fail, match="no such user"):
            GroupMemberRes({"user": "ghost", "group": "kvm"}).probe(ctx)


class TestPython:
    def test_hook_returns_probe_and_actions(self, sh):
        calls = []

        def hook(ctx, step, present):
            calls.append((step["function"], present))
            return Probe(False, "unset"), [Action("set it", ["smbpasswd"], interactive=True)]

        ctx = Ctx(sh, "demo", hooks={"password": hook})
        res = PythonRes({"function": "password", "label": "SMB password"})
        p, actions = plan(res, ctx)
        assert p == Probe(False, "unset")
        assert res.key == "python:SMB password"
        assert calls == [("password", True)]
        assert actions[0].interactive

    def test_hook_may_return_just_a_probe(self, sh):
        ctx = Ctx(sh, "demo", hooks={"pre": lambda *_: Probe(True, "fine")})
        assert plan(PythonRes({"function": "pre"}), ctx) == (Probe(True, "fine"), [])

    def test_missing_hook_fails(self, ctx):
        with pytest.raises(Fail, match="no function 'nope'"):
            PythonRes({"function": "nope"}).probe(ctx)


class TestEngine:
    def test_unknown_is_left_alone_and_changes_are_recorded(self, ctx):
        class Unknown(Resource):
            kind = "x"

            def probe(self, ctx):  # noqa: ARG002
                return Probe(None, "unknown until sudo")

            def plan(self, ctx, p):  # noqa: ARG002
                return [Action("never", ["false"])]

        res = [Unknown({}), UnitRes({"name": "a.service"})]
        planned = probe_and_plan(res, ctx)
        assert planned[0].actions == []
        assert len(planned[1].actions) == 2
        assert ctx.changing == {"unit:a.service"}

    def test_check_converged_fails_on_anything_not_ok(self, ctx, sh, capsys):
        with pytest.raises(Fail, match="not converged: unit:a.service"):
            check_converged([UnitRes({"name": "a.service"})], ctx)
        assert "NOT CONVERGED" in capsys.readouterr().out
        sh.unit("a.service")
        check_converged([UnitRes({"name": "a.service"})], ctx)
