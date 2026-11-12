"""The kvm and windows-vm recipes: what they render, block and do in a fake run."""

import base64
import getpass
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import build_single_file
from sux.install import resources
from sux.install.facts import FACTS, Facts
from sux.install.flow import dump_toml, install_recipe
from sux.install.log import Fail
from sux.install.model import Ctx
from sux.install.recipe import (
    RECIPES_DIR,
    Recipe,
    Values,
    build_resources,
    build_status,
    build_verify,
    load_recipe,
    resolve,
)
from sux.install.resources import DOMAIN_NS, SECRET_MARK

from .conftest import FakeLibvirt, FakeShell
from .test_install_flow import scripted

pytestmark = pytest.mark.usefixtures("sandbox")

IPS = ["100.64.0.7", "fd7a::7"]
NVIDIA = {
    "address": "0000:01:00.0", "vendor": "nvidia", "driver": "nvidia",
    "iommu_group": "12", "device": "0x2f04",
    "group_devices": ["0000:01:00.0", "0000:01:00.1"],
    "functions": ["0000:01:00.0", "0000:01:00.1"],
    "connectors": {"DP-5": "connected"}, "drm": ["card2", "renderD129"],
}  # fmt: skip
INTEL = {
    "address": "0000:00:02.0", "vendor": "intel", "driver": "i915",
    "iommu_group": "0", "device": "0xa780", "group_devices": ["0000:00:02.0"],
    "functions": ["0000:00:02.0"], "connectors": {"HDMI-A-1": "disconnected"},
    "drm": ["card1", "renderD128"],
}  # fmt: skip


@pytest.fixture
def gpus():
    """The fake machine's GPUs; a test changes them in place."""
    return [json.loads(json.dumps(INTEL)), json.loads(json.dumps(NVIDIA))]


@pytest.fixture
def facts(sh, monkeypatch, gpus):
    fixed = {
        "invoking_user": "alice",
        "invoking_group": "staff",
        "tailscale_ips": list(IPS),
        "magicdns": "box.example.ts.net",
        "gpu_devices": gpus,
        "performance_cpus": list(range(16)),
        "memory_gib": 128,
        "iommu": True,
        "virtualization": "vmx",
        "arch": "amd64",
        "home": os.environ["HOME"],  # the sandbox's
    }
    for name, value in fixed.items():
        monkeypatch.setitem(FACTS, name, lambda _sh, value=value: value)
    sh.replies["virsh"] = (127, "")  # no libvirt yet
    return Facts(sh)


@pytest.fixture
def recipe():
    return load_recipe("windows-vm")


def vm(name="win11", gpu="none", **more):
    row = {
        "name": name, "iso": "/srv/Win11.iso", "edition": "Windows 11 Pro",
        "product_key": "", "vcpus": 12, "memory_gib": 32, "disk_gib": 120,
        "disk_dir": "/var/lib/libvirt/images", "username": "alice", "rdp": True,
        "rdp_port": 3389, "guest_ip": "192.168.122.60", "gpu": gpu, "purge": False,
    }  # fmt: skip
    row |= {"reset_method": ""} if gpu == "nvidia" else {}
    row |= {"igd_rom": "/srv/igd.rom"} if gpu == "igpu" else {}
    return row | more


def build(recipe, facts, *rows, **kwargs):
    values, _, unanswered = resolve(recipe, facts, {"vm": list(rows)})
    assert unanswered == []
    return build_resources(recipe, values, **kwargs)


def domains(resources_):
    return [r for r in resources_ if r.kind == "libvirt_domain"]


def devices(xml, tag):
    return ET.fromstring(xml).findall(f"devices/{tag}")


# ---------------------------------------------------------------- kvm


class TestKvm:
    def test_no_questions_and_no_vm(self, facts):
        recipe = load_recipe("kvm")
        values, answers, unanswered = resolve(recipe, facts, {})
        assert (answers, unanswered) == ({}, [])
        keys = [r.key for r in build_resources(recipe, values)]
        assert keys[0] == "python:preflight"
        assert "group_member:alice in libvirt" in keys
        assert "group_member:alice in kvm" in keys
        assert keys[-2:] == ["unit:libvirtd", "libvirt_network:default"]
        packages = next(k for k in keys if k.startswith("apt:"))
        for name in ("qemu-system-x86", "libvirt-daemon-system", "ovmf", "swtpm-tools"):
            assert name in packages
        assert not any("domain" in k for k in keys)

    def test_preflight(self, facts, sh, monkeypatch):
        recipe = load_recipe("kvm")
        hooks = recipe.hooks()
        ctx = Ctx(sh, "kvm", values=Values(facts))
        hooks["preflight"].__globals__["KVM"] = Path("/dev/null")
        assert hooks["preflight"](ctx, {}, True).have == "vmx, IOMMU on"
        monkeypatch.setitem(FACTS, "virtualization", lambda _sh: "")
        ctx = Ctx(sh, "kvm", values=Values(Facts(sh)))
        with pytest.raises(Fail, match="no hardware virtualization"):
            hooks["preflight"](ctx, {}, True)

    def test_network_check(self, facts, sh):
        hooks = load_recipe("kvm").hooks()
        ctx = Ctx(sh, "kvm", values=Values(facts))
        libvirt = FakeLibvirt(sh)
        assert "active on virbr0" in hooks["network_active"](ctx, {"network": "default"})
        libvirt.active = False
        with pytest.raises(Fail, match="not active"):
            hooks["network_active"](ctx, {"network": "default"})


# ---------------------------------------------------------------- the recipe


class TestRecipe:
    def test_needs_kvm_and_has_five_stages(self, recipe):
        assert recipe.needs == ["kvm"]
        names = [stage["name"] for stage in recipe.stages]
        assert names == ["windows", "rdp", "rehearsal", "gpu", "looking-glass"]
        assert [s.get("confirm", False) for s in recipe.stages] == [
            False, False, True, True, False,
        ]  # fmt: skip

    def test_questions_and_their_defaults_come_from_the_machine(self, recipe, facts):
        _, _, unanswered = resolve(recipe, facts, {})
        asked = {q.name: q for q in unanswered}
        assert list(asked) == [
            "name", "iso", "edition", "product_key", "vcpus", "memory_gib",
            "disk_gib", "disk_dir", "username", "rdp", "rdp_port", "guest_ip",
            "gpu", "purge",
        ]  # fmt: skip
        assert asked["vcpus"].default == 12  # 16 performance threads, 4 left
        assert asked["memory_gib"].default == 32
        assert asked["username"].default == "alice"
        assert asked["guest_ip"].default.startswith("192.168.122.")
        assert asked["iso"].default is None  # has to be answered
        assert (asked["gpu"].default, asked["purge"].default) == ("none", False)

    def test_only_gpus_that_can_be_passed_on_are_offered(self, recipe, facts, gpus):
        choices = lambda: {  # noqa: E731
            q.name: q for q in resolve(recipe, Facts(facts.sh), {})[2]
        }["gpu"].choices
        assert choices() == ["none", "igpu", "nvidia"]
        gpus[1]["group_devices"].append("0000:01:00.2")  # shares its IOMMU group
        assert choices() == ["none", "igpu"]
        del gpus[:]
        assert choices() == ["none"]

    def test_gpu_specific_questions(self, recipe, facts, gpus):
        def asked(row):
            return [q.name for q in resolve(recipe, Facts(facts.sh), {"vm": [row]})[2]]

        row = vm(gpu="nvidia")
        del row["reset_method"]
        assert asked(row) == ["reset_method"]
        row = vm(gpu="igpu")
        del row["igd_rom"]
        assert asked(row) == ["igd_rom"]
        # Looking Glass: only with the NVIDIA card and a display on the Intel GPU
        gpus[0]["connectors"]["HDMI-A-1"] = "connected"
        assert asked(vm(gpu="nvidia")) == ["looking_glass"]
        assert asked(vm(gpu="igpu")) == []
        on = vm(gpu="nvidia", looking_glass=True)
        assert asked(on) == ["lg_installer", "fullscreen", "shm_mib"]

    def test_cpu_pinning_and_stable_mac(self, recipe, facts):
        values, _, _ = resolve(recipe, facts, {"vm": [vm(vcpus="8")]})
        one = values["vm"][0]
        assert one["pins"][0] == {"vcpu": 0, "cpu": 8}
        assert one["pins"][-1] == {"vcpu": 7, "cpu": 15}
        assert (one["cores"], one["emulator_cpus"]) == (4, "0,1,2,3,4,5,6,7")
        assert re.fullmatch(r"52:54:00(:[0-9a-f]{2}){3}", one["mac"])
        again, _, _ = resolve(recipe, facts, {"vm": [vm(vcpus="8")]})
        assert again["vm"][0]["mac"] == one["mac"]

    def test_what_each_stage_manages(self, recipe, facts):
        row = vm(gpu="nvidia")
        keys = {
            stage["name"]: [
                r.key for r in build(recipe, facts, row, stage=stage["name"]) if r.present
            ]
            for stage in recipe.stages
        }
        assert keys["windows"][0] == "python:preflight win11"
        assert keys["windows"][-2:] == ["libvirt_domain:win11", "python:Windows installed in win11"]
        assert "secret:/var/lib/winvm/win11/unattend/autounattend.xml" in keys["windows"]
        assert "image:/var/lib/libvirt/images/win11.qcow2" in keys["windows"]
        assert keys["rdp"] == [
            "file:/etc/systemd/system/winvm-rdp-win11.socket",
            "file:/etc/systemd/system/winvm-rdp-win11.service",
            "systemd:daemon-reload",
            "unit:winvm-rdp-win11.socket",
            "unit:winvm-rdp-win11.service",
        ]
        assert keys["rehearsal"] == [
            "file:/etc/libvirt/hooks/qemu.d/50-winvm",
            "systemd:daemon-reload",
            "unit:libvirtd",
            "file:/etc/udev/rules.d/61-winvm-mutter.rules",
            "python:udev rules loaded",
            "python:rehearsal: nvidia GPU (win11)",
        ]
        assert keys["gpu"] == [
            "libvirt_domain:win11",
            "python:Windows in win11 reports the nvidia GPU",
        ]
        assert keys["looking-glass"] == []  # nothing is built unless it is asked for
        none = [r.key for r in build(recipe, facts, vm()) if r.present]
        assert not any("hook" in k or "udev" in k or "rehearsal" in k for k in none)

    def test_looking_glass_is_built_only_when_asked_for(self, recipe, facts, gpus):
        gpus[0]["connectors"]["HDMI-A-1"] = "connected"
        row = vm(gpu="nvidia", looking_glass=True, lg_installer="/srv/idd.exe",
                 fullscreen=True, shm_mib=64)  # fmt: skip
        keys = [r.key for r in build(recipe, facts, row) if r.present]
        assert "python:looking-glass-client B8-rc1" in keys
        assert "file:/etc/tmpfiles.d/10-looking-glass-win11.conf" in keys
        final = domains(build(recipe, facts, row, stage="gpu"))[0].content
        (shmem,) = devices(final, "shmem")
        assert shmem.get("name") == "looking-glass-win11"
        assert shmem.findtext("size") == "64"
        tmpfiles = next(r for r in build(recipe, facts, row) if "tmpfiles" in r.key)
        assert "f /dev/shm/looking-glass-win11 0660 alice kvm -" in tmpfiles.content
        removed = build(recipe, facts, row, remove=True)
        assert not any(r.kind == "apt" for r in removed)  # compilers are not removed

    def test_removal_keeps_the_disk_unless_purge_is_set(self, recipe, facts):
        gone = {r.key: r for r in build(recipe, facts, vm(), remove=True)}
        assert not any(r.present for r in gone.values() if r.kind != "systemd")
        assert gone["image:/var/lib/libvirt/images/win11.qcow2"].purge is False
        assert gone["libvirt_domain:win11"].purge is False
        assert "unit:libvirtd" not in gone  # the daemon is kvm's
        purged = {r.key: r for r in build(recipe, facts, vm(purge=True), remove=True)}
        assert purged["image:/var/lib/libvirt/images/win11.qcow2"].purge is True
        assert purged["libvirt_domain:win11"].purge is True
        assert not any("Win11.iso" in key for key in gone)  # the ISO is never ours

    def test_a_removed_vm_takes_only_its_own_things(self, recipe, facts):
        rows = [vm(), vm("cad", rdp_port=3390, guest_ip="192.168.122.61") | {"removed": True}]
        res = {r.key: r for r in build(recipe, facts, *rows)}
        assert res["file:/etc/winvm/cad.conf"].present is False
        assert res["file:/etc/winvm/win11.conf"].present is True
        assert res["file:/usr/local/bin/winvm"].present is True

    def test_status_covers_what_a_diagnosis_needs(self, recipe, facts):
        values, _, _ = resolve(recipe, facts, {"vm": [vm()]})
        lines = [" ".join(e["argv"]) for e in build_status(recipe, values)]
        for needle in (
            "dominfo win11", "domstate --reason win11", "dumpxml win11",
            "tail -n 150 /var/log/libvirt/qemu/win11.log", "journalctl -u libvirtd",
            "journalctl -t winvm", "vfio|nvidia|NVRM|i915|iommu|DMAR",
            "lspci -nnk -d ::0300", "nvidia-smi",
        ):  # fmt: skip
            assert any(needle in line for line in lines), needle

    def test_pinned_artifacts(self, recipe, facts):
        res = {r.key: r for r in build(recipe, facts, vm())}
        iso = res["download:/var/lib/libvirt/images/virtio-win-0.1.302.iso"]
        assert iso.url.endswith("/archive-virtio/virtio-win-0.1.302-1/virtio-win-0.1.302.iso")
        assert re.fullmatch(r"[0-9a-f]{64}", iso.sha256)
        assert re.fullmatch(r"[0-9a-f]{40}", recipe.variables["lg_commit"])

    def test_nothing_of_this_machine_is_in_the_recipes(self, recipe):
        private = {socket.gethostname(), getpass.getuser(), str(Path.home())}
        private = {value for value in private if len(value) > 3 and value != "root"}
        real = Facts(FakeShell())  # reads only /sys of the machine the tests run on
        cards = [gpu["address"] for gpu in real.get("gpus")]
        for tree in (recipe.tree, load_recipe("kvm").tree):
            for name, text in tree.items():
                for value in private:
                    assert not re.search(rf"\b{re.escape(value)}\b", text), (name, value)
                for address in cards:
                    assert address not in text, (name, address)
                assert not re.search(r"\b100\.\d+\.\d+\.\d+\b", text), name


# ---------------------------------------------------------------- rendering


class TestDomainXml:
    def final(self, recipe, facts, row):
        return domains(build(recipe, facts, row, stage="gpu"))[0].content

    def test_at_rest_there_is_no_passed_through_device(self, recipe, facts):
        xml = self.final(recipe, facts, vm())
        root = ET.fromstring(xml)
        assert devices(xml, "hostdev") == []
        assert [d.get("device") for d in devices(xml, "disk")] == ["disk"]
        assert root.find("os").get("firmware") == "efi"
        assert root.find("os/type").get("machine") == "q35"
        features = {f.get("name"): f.get("enabled") for f in root.findall("os/firmware/feature")}
        assert features == {"secure-boot": "yes", "enrolled-keys": "yes"}
        tpm = root.find("devices/tpm")
        assert (tpm.get("model"), tpm.find("backend").get("version")) == ("tpm-crb", "2.0")
        assert root.find("features/hyperv/synic").get("state") == "on"
        assert root.find("cpu").get("mode") == "host-passthrough"
        assert len(root.findall("cputune/vcpupin")) == 12 == int(root.findtext("vcpu"))
        assert root.find("cpu/topology").get("cores") == "6"
        assert root.find("devices/interface/model").get("type") == "virtio"
        assert root.find("devices/channel/target").get("name") == "org.qemu.guest_agent.0"
        assert root.find("devices/disk/source").get("file").endswith("/win11.qcow2")

    def test_nvidia_adds_the_card_and_its_audio(self, recipe, facts):
        xml = self.final(recipe, facts, vm(gpu="nvidia"))
        found = devices(xml, "hostdev")
        assert [h.get("managed") for h in found] == ["yes", "yes"]
        where = [h.find("source/address").attrib for h in found]
        assert where[0] == {"domain": "0x0000", "bus": "0x01", "slot": "0x00", "function": "0x0"}
        assert where[1]["function"] == "0x1"
        assert all(h.find("rom") is None for h in found)

    def test_igpu_adds_the_intel_gpu_with_its_rom_at_00_02(self, recipe, facts):
        xml = self.final(recipe, facts, vm(gpu="igpu"))
        (dev,) = devices(xml, "hostdev")
        assert dev.find("source/address").get("slot") == "0x02"
        assert dev.find("rom").get("file") == "/srv/igd.rom"
        guest = dev.find("address").attrib
        assert (guest["bus"], guest["slot"], guest["function"]) == ("0x00", "0x02", "0x0")

    def test_the_install_definition_has_the_three_cds_and_no_gpu(self, recipe, facts):
        (install,) = domains(build(recipe, facts, vm(gpu="nvidia"), stage="windows"))
        cds = [d for d in devices(install.content, "disk") if d.get("device") == "cdrom"]
        assert [cd.find("source").get("file") for cd in cds] == [
            "/srv/Win11.iso",
            "/var/lib/libvirt/images/virtio-win-0.1.302.iso",
            "/var/lib/libvirt/images/win11-unattend.iso",
        ]
        assert devices(install.content, "hostdev") == []
        orders = [d.find("boot").get("order") for d in devices(install.content, "disk")[:2]]
        assert orders == ["1", "2"]  # the empty disk first, so later boots skip the CD

    def test_once_installed_only_the_final_definition_is_managed(
        self, recipe, facts, tmp_path
    ):
        row = vm(disk_dir=str(tmp_path))
        assert len(domains(build(recipe, facts, row))) == 2
        (tmp_path / "win11.qcow2.installed").touch()
        (only,) = domains(build(recipe, facts, row))
        assert devices(only.content, "disk")[0].get("device") == "disk"
        assert len(devices(only.content, "disk")) == 1
        (still,) = domains(build(recipe, facts, row, remove=True))
        assert still.present is False

    def test_two_vms_with_different_gpus(self, recipe, facts):
        rows = [vm(gpu="nvidia"), vm("cad", "igpu", rdp_port=3390, guest_ip="192.168.122.61")]
        win11, cad = domains(build(recipe, facts, *rows, stage="gpu"))
        assert len(devices(win11.content, "hostdev")) == 2
        assert len(devices(cad.content, "hostdev")) == 1
        rule = next(r for r in build(recipe, facts, *rows) if "udev" in r.key and r.kind == "file")
        assert rule.content.count('TAG+="mutter-device-ignore"') == 2
        assert 'KERNELS=="0000:00:02.0", PROGRAM=="/usr/local/bin/winvm display-elsewhere' in rule.content


class TestAnswerFile:
    @pytest.fixture
    def answer(self, recipe, facts):
        def render(**more):
            res = build(recipe, facts, vm(**more), stage="windows")
            return next(r for r in res if r.kind == "secret")

        return render

    def test_well_formed_with_account_drivers_and_rdp(self, answer):
        res = answer()
        assert res.meta == "root:root 0600"
        assert res.content.count(SECRET_MARK) == 2  # the account, and its first logon
        assert res.content.startswith('<?xml version="1.0" encoding="utf-8"?>')
        assert res.content.isascii()
        text = res.content.replace(SECRET_MARK, "pw")
        root = ET.fromstring(text.encode())
        ns = {"u": "urn:schemas-microsoft-com:unattend"}
        passes = [s.get("pass") for s in root.findall("u:settings", ns)]
        assert passes == ["windowsPE", "specialize", "oobeSystem"]
        paths = [p.text for p in root.iter("{urn:schemas-microsoft-com:unattend}Path")]
        assert r"E:\viostor\w11\amd64" in paths
        assert r"E:\NetKVM\w11\amd64" in paths
        find = lambda tag: [e.text for e in root.iter(f"{{{ns['u']}}}{tag}")]  # noqa: E731
        assert find("fDenyTSConnections") == ["false"]
        assert find("Group") == ["@FirewallAPI.dll,-28752", "Administrators"]
        assert find("Name") == ["alice"]
        assert find("ComputerName") == ["win11"]
        assert find("Value") == ["Windows 11 Pro", "pw", "pw"]
        assert find("HideOnlineAccountScreens") == ["true"]
        assert "setup.cmd" in find("CommandLine")[0]
        assert find("Key") == ["/IMAGE/NAME"]  # no product key unless one is given

    def test_product_key_only_when_given(self, answer):
        text = answer(product_key="AAAAA-BBBBB-CCCCC-DDDDD-EEEEE").content
        assert "<ProductKey><Key>AAAAA-BBBBB-CCCCC-DDDDD-EEEEE</Key>" in text
        ET.fromstring(text.replace(SECRET_MARK, "pw").encode())

    def test_first_logon_script(self, recipe, facts):
        res = build(recipe, facts, vm(), stage="windows")
        script = next(r for r in res if r.target.endswith("setup.cmd")).content
        assert "bEnumerateHWBeforeSW /t REG_DWORD /d 1" in script
        assert "AVCHardwareEncodePreferred" in script
        last = script.strip().splitlines()[-1]
        assert "virtio-win-guest-tools.exe /install /passive /norestart" in last


# ---------------------------------------------------------------- hooks


@pytest.fixture
def hooks(recipe, tmp_path, monkeypatch):
    found = recipe.hooks()
    scope = found["preflight"].__globals__
    scope["_driver"] = lambda address: {"0000:01:00.0": "nvidia"}.get(address, "i915")
    scope["PROC"] = tmp_path / "proc"
    scope["STATE"] = tmp_path / "state"
    scope["_sleep"] = lambda _s: None
    (tmp_path / "proc").mkdir()
    (tmp_path / "state").mkdir()
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
    return found


@pytest.fixture
def hctx(recipe, sh, facts):
    def make(*rows):
        values, _, _ = resolve(recipe, facts, {"vm": list(rows or [vm()])})
        return Ctx(sh, "windows-vm", values=values, hooks=recipe.hooks())

    return make


def step_for(recipe, ctx, label):
    """The rendered step of the python resource with this label."""
    res = build_resources(recipe, ctx.values)
    return next(r for r in res if r.kind == "python" and r.target == label).step


def run_blocked(result):
    probe, actions = result
    assert probe.ok is False
    with pytest.raises(Fail) as e:
        actions[0].fn()
    return probe.have, str(e.value)


class TestPreflight:
    def step(self, tmp_path, **more):
        iso = tmp_path / "Win11.iso"
        iso.touch()
        row = vm(iso=str(iso), disk_dir=str(tmp_path), disk_gib=1, **more)
        return row | {"vm": row["name"], "disk": f"{tmp_path}/win11.qcow2", "installed": False}

    def test_answers_that_cannot_work_stop_the_run(self, hooks, hctx, tmp_path):
        ctx = hctx()
        cases = [
            ({"name": "bad name"}, "VM name 'bad name'"),
            ({"vcpus": 5}, "vCPUs must be an even number from 2 to 16"),
            ({"vcpus": 32}, "vCPUs must be an even number"),
            ({"memory_gib": 128}, "128 GiB is all the memory there is"),
            ({"guest_ip": "nonsense"}, "guest address"),
            ({"username": "a b"}, "Windows user name"),
            ({"gpu": "amd"}, "there is no amd GPU that can be passed on"),
        ]
        for change, message in cases:
            step = self.step(tmp_path) | change
            step["vm"] = step["name"]
            with pytest.raises(Fail, match=message):
                hooks["preflight"](ctx, step, True)

    def test_two_vms_cannot_share_an_address_or_a_port(self, hooks, hctx, tmp_path):
        ctx = hctx(vm(), vm("cad"))
        with pytest.raises(Fail, match="another VM has the address 192.168.122.60"):
            hooks["preflight"](ctx, self.step(tmp_path), True)
        ctx = hctx(vm(), vm("cad", guest_ip="192.168.122.61"))
        with pytest.raises(Fail, match="another VM forwards RDP port 3389"):
            hooks["preflight"](ctx, self.step(tmp_path), True)
        ctx = hctx(vm(), vm("cad", guest_ip="192.168.122.61", rdp_port=3390))
        hooks["preflight"].__globals__["_install_blocker"] = lambda _step: None
        assert hooks["preflight"](ctx, self.step(tmp_path), True).ok is True

    def test_a_missing_iso_blocks_its_stage_not_the_plan(self, hooks, hctx, tmp_path):
        step = self.step(tmp_path) | {"iso": str(tmp_path / "nope.iso")}
        have, error = run_blocked(hooks["preflight"](hctx(), step, True))
        assert have == f"BLOCKED: no ISO at {tmp_path}/nope.iso"
        assert "win11: there is no Windows ISO at" in error

    def test_an_iso_qemu_cannot_reach_and_a_full_disk(self, hooks, hctx, tmp_path):
        closed = tmp_path / "private"
        closed.mkdir(mode=0o700)
        (closed / "Win11.iso").touch()
        step = self.step(tmp_path) | {"iso": str(closed / "Win11.iso")}
        _, error = run_blocked(hooks["preflight"](hctx(), step, True))
        assert f"the ISO is under {closed}, which libvirt's QEMU user cannot enter" in error
        tmp_path.chmod(0o755)
        for parent in tmp_path.parents:
            if not parent.stat().st_mode & 0o001:
                pytest.skip("the test directory itself is closed to other users")
        step = self.step(tmp_path) | {"disk_gib": 10**9}
        _, error = run_blocked(hooks["preflight"](hctx(), step, True))
        assert "disk does not fit" in error
        assert hooks["preflight"](hctx(), step | {"installed": True}, True).ok is True


class TestGpuBlockers:
    """Why a GPU cannot be handed over; shown in the plan, fails only its stage."""

    def rehearsal(self, recipe, hooks, hctx, row):
        ctx = hctx(row)
        label = f"rehearsal: {row['gpu']} GPU ({row['name']})"
        return hooks["gpu_rehearsal"](ctx, step_for(recipe, ctx, label), True)

    def test_display_on_the_nvidia_card_only_and_run_from_the_desktop(
        self, recipe, hooks, hctx, sh, monkeypatch
    ):
        sh.unit("display-manager.service")
        monkeypatch.delenv("SSH_CONNECTION")
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        result = self.rehearsal(recipe, hooks, hctx, vm(gpu="nvidia"))
        have, error = run_blocked(result)
        assert have == "BLOCKED: this terminal is part of the desktop that gets stopped"
        assert "Run this over SSH, in tmux, or on a text console" in error
        assert "connect a display to an Intel output" in error

    def test_over_ssh_the_plan_warns_that_the_screen_goes_blank(
        self, recipe, hooks, hctx, sh
    ):
        sh.unit("display-manager.service")
        probe, (action,) = self.rehearsal(recipe, hooks, hctx, vm(gpu="nvidia"))
        assert probe.have == "not rehearsed"
        assert "THE LOCAL SCREEN GOES BLANK" in "\n".join(action.detail)
        assert any(
            line.endswith("winvm rehearse 0000:01:00.0 0000:01:00.1 --unload "
                          "nvidia_drm nvidia_modeset nvidia_uvm")
            for line in action.detail
        )  # fmt: skip

    def test_with_a_display_on_intel_the_desktop_must_have_let_go(
        self, recipe, hooks, hctx, sh, gpus, tmp_path
    ):
        sh.unit("display-manager.service")
        gpus[0]["connectors"]["HDMI-A-1"] = "connected"
        probe, (action,) = self.rehearsal(recipe, hooks, hctx, vm(gpu="nvidia"))
        assert "BLANK" not in "\n".join(action.detail)  # the desktop stays up
        fds = tmp_path / "proc" / "4343" / "fd"
        fds.mkdir(parents=True)
        (tmp_path / "proc" / "4343" / "comm").write_text("gnome-shell\n")
        (fds / "9").symlink_to("/dev/dri/card2")
        have, error = run_blocked(self.rehearsal(recipe, hooks, hctx, vm(gpu="nvidia")))
        assert have == "BLOCKED: held by gnome-shell (pid 4343)"
        assert "log out and in, or reboot, then run this again" in error

    def test_igpu_needs_its_rom_and_no_display_on_it(
        self, recipe, hooks, hctx, gpus, tmp_path
    ):
        row = vm(gpu="igpu", igd_rom=str(tmp_path / "igd.rom"))
        have, error = run_blocked(self.rehearsal(recipe, hooks, hctx, row))
        assert have.startswith("BLOCKED: no option ROM at")
        assert "github.com/tomitamoeko/VfioIgdPkg" in error
        (tmp_path / "igd.rom").touch()
        probe, (action,) = self.rehearsal(recipe, hooks, hctx, row)
        assert action.desc == "rehearse handing the igpu GPU (0000:00:02.0) over and back"
        assert not any("--unload" in line for line in action.detail)
        gpus[0]["connectors"]["HDMI-A-1"] = "connected"
        have, error = run_blocked(self.rehearsal(recipe, hooks, hctx, row))
        assert have == "BLOCKED: a display is connected to it (HDMI-A-1)"
        assert "only be passed on while the host does not use it" in error

    def test_a_card_in_a_running_guest_and_a_card_that_is_gone(
        self, recipe, hooks, hctx, gpus
    ):
        hooks["preflight"].__globals__["_driver"] = lambda _a: "vfio-pci"
        have, _ = run_blocked(self.rehearsal(recipe, hooks, hctx, vm(gpu="nvidia")))
        assert have == "BLOCKED: in use by a running guest"
        ctx = hctx(vm(gpu="nvidia"))
        step = step_for(recipe, ctx, "rehearsal: nvidia GPU (win11)")
        del gpus[1]
        ctx = hctx(vm())
        have, _ = run_blocked(hooks["gpu_rehearsal"](ctx, step, True))
        assert have == "BLOCKED: no nvidia GPU that can be passed on"

    def test_one_rehearsal_per_card_and_none_once_it_passed(
        self, recipe, hooks, hctx, tmp_path
    ):
        rows = [vm(gpu="nvidia"), vm("cad", "nvidia", rdp_port=3390, guest_ip="192.168.122.61")]
        ctx = hctx(*rows)
        first = hooks["gpu_rehearsal"](ctx, step_for(recipe, ctx, "rehearsal: nvidia GPU (win11)"), True)
        second = hooks["gpu_rehearsal"](ctx, step_for(recipe, ctx, "rehearsal: nvidia GPU (cad)"), True)
        assert first[0].ok is False
        assert (second.ok, second.have) == (True, "as above")
        (tmp_path / "state" / "rehearsed-0000:01:00.0-0x2f04").touch()
        ctx = hctx(*rows)
        again = hooks["gpu_rehearsal"](ctx, step_for(recipe, ctx, "rehearsal: nvidia GPU (win11)"), True)
        assert (again.ok, again.have) == (True, "passed for 0000:01:00.0")


class TestChecks:
    def test_rdp_is_proven_by_an_rdp_answer_not_an_open_port(
        self, hooks, hctx, sh, monkeypatch
    ):
        libvirt = FakeLibvirt(sh)
        libvirt.domains["win11"], libvirt.states["win11"] = "<domain/>", "shut off"
        entry = {"vm": "win11", "port": 3389, "addresses": IPS}
        ctx = hctx()
        assert hooks["rdp_reachable"](ctx, entry) == "win11 is not running: RDP not checked"
        libvirt.states["win11"] = "running"
        sent, replies = [], [b"\x03\x00\x00\x13\x0e\xd0", b"\x03\x00\x00\x13\x0e\xd0"]

        class Conn:
            def __init__(self, where, timeout):  # noqa: ARG002
                sent.append(where)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def sendall(self, data):
                sent.append(data)

            def recv(self, _n):
                return replies.pop(0)

        scope = hooks["rdp_reachable"].__globals__
        monkeypatch.setattr(scope["socket"], "create_connection", Conn)
        message = hooks["rdp_reachable"](ctx, entry)
        assert message == "win11: an RDP server answers on 100.64.0.7, fd7a::7 port 3389"
        assert sent[0] == ("100.64.0.7", 3389)
        assert sent[1][:4] == b"\x03\x00\x00\x13"  # TPKT, 19 bytes
        assert len(sent[1]) == 19
        replies[:] = [b""]  # the forward accepts, nothing answers behind it
        with pytest.raises(Fail, match="100.64.0.7 port 3389 is open, but not RDP"):
            hooks["rdp_reachable"](ctx, entry)

    def test_connect_help_names_host_port_user_and_password(self, hooks, hctx, tmp_path):
        answers = tmp_path / "autounattend.xml"
        answers.write_text("<Value>Windows 11 Pro</Value>\n<Value>s3cret-s3cret</Value>")
        entry = {"vm": "win11", "port": 3389, "addresses": IPS, "username": "alice",
                 "answers": str(answers)}  # fmt: skip
        answers.write_text("<Password><Value>s3cret-s3cret</Value></Password>")
        line = hooks["connect_help"](hctx(), entry)
        assert line == (
            "win11 over RDP: box.example.ts.net:3389  user alice  password s3cret-s3cret"
        )

    def test_gpu_on_the_host_or_in_the_guest(self, hooks, hctx, sh):
        libvirt = FakeLibvirt(sh)
        libvirt.domains["win11"], libvirt.states["win11"] = "<domain/>", "shut off"
        ctx = hctx(vm(gpu="nvidia"))
        entry = {"vm": "win11", "gpu": "nvidia"}
        hooks["gpu_host"].__globals__["_driver"] = lambda a: "nvidia"
        assert hooks["gpu_host"](ctx, entry) == "nvidia GPU is on the host (nvidia)"
        hooks["gpu_host"].__globals__["_driver"] = lambda a: "vfio-pci"
        with pytest.raises(Fail, match="on vfio-pci although no VM runs"):
            hooks["gpu_host"](ctx, entry)
        libvirt.states["win11"] = "running"
        assert hooks["gpu_host"](ctx, entry).startswith("nvidia GPU is in win11")
        hooks["gpu_host"].__globals__["_driver"] = lambda a: ""
        libvirt.states["win11"] = "shut off"
        with pytest.raises(Fail, match="has no driver on the host"):
            hooks["gpu_host"](ctx, entry)


# ---------------------------------------------------------------- a fake run


def adapters(*rows):
    """A guest agent that answers, with these display adapters in Windows."""
    text = "".join(f"{device}|{code}|{name}\r\n" for device, code, name in rows)

    def answer(command):
        if command["execute"] == "guest-exec":
            return {"pid": 7}
        if command["execute"] == "guest-exec-status":
            return {"exited": True, "exitcode": 0,
                    "out-data": base64.b64encode(text.encode()).decode()}  # fmt: skip
        if command["execute"] == "guest-get-osinfo":
            return {"pretty-name": "Windows 11 Pro"}
        return {}

    return answer


BASIC = ("PCI\\VEN_1234&DEV_1111", "0", "Microsoft Basic Display Adapter")
RTX = ("PCI\\VEN_10DE&DEV_2F04", "0", "NVIDIA GeForce RTX 5070")
UHD = ("PCI\\VEN_8086&DEV_A780", "0", "Intel(R) UHD Graphics 770")


class Machine:
    """The recipes with every path moved under a temporary root, and a fake host."""

    def __init__(self, tmp_path, sandbox, monkeypatch):
        self.root = tmp_path / "root"
        self.recipes = tmp_path / "recipes"
        self.images = self.root / "var/lib/libvirt/images"
        self.images.mkdir(parents=True)
        self.state = sandbox / "state" / "sux" / "install"
        self.units, self.shells = {}, []
        self.libvirt = None
        self.rehearse = (0, "rehearsal passed")
        self.guest = adapters(BASIC)
        self.drivers = {}
        content = b"virtio-win"
        for name in ("kvm", "windows-vm"):
            for path in (RECIPES_DIR / name).rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts:
                    dest = self.recipes / name / path.relative_to(RECIPES_DIR / name)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(self.moved(path.read_text(), sandbox, content))
        monkeypatch.setattr(
            resources, "fetch", lambda ctx, url, dest, sha256=None: self.fetch(dest, content)
        )
        # files are written as this user; the recipe wants them owned by root
        stat_meta = resources.stat_meta

        def owned_by_root(ctx, path):
            exists, meta = stat_meta(ctx, path)
            return exists, re.sub(r"^\S+:\S+ ", "root:root ", meta)

        monkeypatch.setattr(resources, "stat_meta", owned_by_root)
        load = Recipe._load_hooks  # noqa: SLF001
        machine = self

        def patched(recipe):
            found = load(recipe)
            scope = found["preflight"].__globals__ if found else {}
            fixed = {
                "_sleep": lambda _s: None,
                "_driver": lambda a: machine.drivers.get(a, "nvidia"),
                "PROC": machine.root / "proc",
                "STATE": machine.root / "var/lib/winvm",
                "WINVM": str(machine.root / "usr/local/bin/winvm"),
                "_install_blocker": lambda _step: None,
                "KVM": Path("/dev/null"),
            }
            scope.update({k: v for k, v in fixed.items() if k in scope})
            if "socket" in scope:
                monkeypatch.setattr(scope["socket"], "create_connection", machine.connect)
            return found

        monkeypatch.setattr(Recipe, "_load_hooks", patched)
        monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")

    def moved(self, text, sandbox, content):
        text = text.replace("/etc/systemd/system", str(sandbox / "units"))
        for top in ("/etc/", "/usr/local/", "/var/lib/", "/opt/", "/dev/shm/"):
            text = re.sub(rf'(?<=["\' =]){top}', f"{self.root}{top}", text)
        sha = re.search(r'virtio_sha256\s*= "([0-9a-f]{64})"', text)
        if sha:
            text = text.replace(sha[1], hashlib.sha256(content).hexdigest())
        return text

    @staticmethod
    def fetch(dest, content):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)

    def connect(self, where, timeout):  # noqa: ARG002
        machine = self

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def sendall(self, data):
                pass

            def recv(self, _n):
                running = "running" in machine.libvirt.states.values()
                return b"\x03\x00\x00\x13" if running else b""

        return Conn()

    def make_shell(self, log):
        sh = FakeShell(log)
        sh.units = self.units
        sh.unit("libvirtd")
        if self.libvirt is None:
            self.libvirt = FakeLibvirt(sh)
        sh.replies["virsh"] = self.libvirt
        for name in list(self.libvirt.domains):
            self.libvirt.agent[name] = lambda command: self.guest(command)
        self.libvirt.agent.setdefault("win11", lambda command: self.guest(command))
        self.libvirt.agent.setdefault("cad", lambda command: self.guest(command))

        def make(index):
            def create(cmd, _stdin):
                Path(cmd[index]).write_text("image")
                return 0, ""

            return create

        def installed(cmd, _stdin):
            return 0, "".join(f"{p} installed\n" for p in cmd[3:])

        def listening(_cmd, _stdin):
            ports = [u.split("-")[2] for u in self.units if u.endswith(".socket")]
            rows = self.ports()
            return 0, "".join(
                f"LISTEN 0 4096 {'[' + ip + ']' if ':' in ip else ip}:{port} *:*\n"
                for port in rows
                for ip in IPS
                if ports
            )

        sh.replies |= {
            "qemu-img create": make(4),
            "xorriso": make(9),
            "dpkg-query": installed,
            "id -nG": (0, "alice libvirt kvm"),
            "ss -ltnH": listening,
            f"{self.root}/usr/local/bin/winvm rehearse": lambda c, s: self.rehearse,
            "nvidia-smi": (0, "ok"),
        }
        self.shells.append(sh)
        return sh

    def ports(self):
        rows = tomllib.loads((self.config / "windows-vm.toml").read_text())["vm"]
        return [row["rdp_port"] for row in rows if row["rdp"] and not row.get("removed")]

    def answers(self, *rows):
        self.config.mkdir(parents=True, exist_ok=True)
        rows = [row | {"disk_dir": str(self.images), "disk_gib": 1} for row in rows]
        (self.config / "windows-vm.toml").write_text(dump_toml({"vm": rows}))

    @property
    def config(self):
        return Path(os.environ["XDG_CONFIG_HOME"]) / "sux" / "install"

    def run(self, *lines, tty=True, **kwargs):
        typed = scripted(*lines)
        code = install_recipe(
            str(self.recipes / "windows-vm"),
            input_fn=typed,
            tty=tty,
            make_shell=self.make_shell,
            **kwargs,
        )
        return code, self.shells[-1], typed.prompts

    def events(self):
        log = sorted(self.state.glob("windows-vm.*.jsonl"))[-1]
        return [json.loads(line) for line in log.read_text().splitlines()]

    def hostdevs(self, name="win11"):
        return devices(self.libvirt.domains[name], "hostdev")

    def cds(self, name="win11"):
        disks = devices(self.libvirt.domains[name], "disk")
        return [d for d in disks if d.get("device") == "cdrom"]


@pytest.fixture
def machine(tmp_path, sandbox, monkeypatch, facts):  # noqa: ARG001
    return Machine(tmp_path, sandbox, monkeypatch)


class TestRun:
    def test_install_without_a_gpu_then_nothing_to_change(self, machine, capsys):
        machine.answers(vm())
        code, sh, prompts = machine.run("a", "a")
        out = capsys.readouterr().out
        assert code == 0, out
        assert len(prompts) == 2  # the plan, and `confirm` before the gpu stage
        lv = machine.libvirt
        assert "ok  kvm (needed by windows-vm): nothing to change" in out
        assert [c[1:] for c in lv.did("start")] == [["win11"]]
        assert len(lv.did("send-key")) == 10  # "Press any key to boot from CD"
        assert (machine.images / "win11.qcow2.installed").exists()
        assert len(lv.did("define")) == 2  # to install, then the one it runs with
        assert machine.hostdevs() == []
        assert machine.cds() == []  # the media, with the password on them, are gone
        assert lv.states["win11"] == "running"
        assert "192.168.122.60" in lv.hosts[0]
        assert machine.units["winvm-rdp-win11.socket"]["active"]
        socket_unit = (machine.root.parent / "host/units/winvm-rdp-win11.socket").read_text()
        assert "ListenStream=100.64.0.7:3389\nListenStream=[fd7a::7]:3389\n" in socket_unit
        assert "0.0.0.0" not in socket_unit
        order = ["Stage 1/5  windows", "Stage 2/5  rdp", "Stage 3/5  rehearsal",
                 "Stage 4/5  gpu", "Stage 5/5  looking-glass"]  # fmt: skip
        places = [out.index(text) for text in order]
        assert places == sorted(places)
        assert "ok  win11 is running: Windows 11 Pro" in out
        assert "ok  win11: an RDP server answers on 100.64.0.7, fd7a::7 port 3389" in out
        password = re.search(r"user alice  password (\S+)", out)[1]
        assert len(password) == 12
        log = "\n".join(json.dumps(e) for e in machine.events())
        assert password not in log  # scrubbed
        assert not any("rehearse" in c for c in sh.commands())
        assert not (machine.root / "etc/libvirt/hooks").exists()  # no GPU: no hook

        code, sh, prompts = machine.run()
        out = capsys.readouterr().out
        assert code == 0, out
        assert prompts == []
        assert sh.changes() == []
        assert out.count("Plan: nothing to change") == 5

    def test_not_a_terminal_prints_the_plan_and_changes_nothing(self, machine, capsys):
        machine.answers(vm(gpu="nvidia"))
        code, sh, _ = machine.run(tty=False)
        out = capsys.readouterr().out
        assert code == 0
        assert sh.changes() == []
        assert machine.libvirt.domains == {}
        assert "rehearse handing the nvidia GPU (0000:01:00.0) over and back" in out
        assert "it stays usable over RDP" in out
        assert "Nothing was changed: stdin is not a terminal." in out

    def test_changing_the_gpu_redefines_and_reinstalls_nothing(self, machine, capsys):
        machine.answers(vm())
        machine.run("a", "a")
        machine.guest = adapters(BASIC, RTX)
        machine.answers(vm(gpu="nvidia"))
        capsys.readouterr()
        code, sh, prompts = machine.run("a", "a", "a")
        out = capsys.readouterr().out
        assert code == 0, out
        assert len(prompts) == 3  # the plan, then `confirm` for each GPU stage
        lv = machine.libvirt
        assert any("winvm rehearse 0000:01:00.0 0000:01:00.1" in c for c in sh.changes())
        assert len(machine.hostdevs()) == 2
        assert len(lv.did("send-key")) == 10  # Windows was not installed again
        assert (machine.root / "etc/libvirt/hooks/qemu.d/50-winvm").stat().st_mode & 0o111
        assert "systemctl restart libvirtd" in sh.changes()  # it reads hooks at start
        assert "ok  win11: Windows reports NVIDIA GeForce RTX 5070 working" in out
        assert lv.states["win11"] == "running"
        assert "gpu = nvidia" in (machine.root / "etc/winvm/win11.conf").read_text()
        code, sh, prompts = machine.run()
        assert (code, prompts, sh.changes()) == (0, [], [])

        machine.answers(vm(gpu="igpu", igd_rom=str(machine.images / "igd.rom")))
        (machine.images / "igd.rom").touch()
        machine.guest = adapters(BASIC, UHD)
        machine.drivers["0000:00:02.0"] = "i915"
        code, sh, _ = machine.run("a", "a", "a")
        out = capsys.readouterr().out
        assert code == 0, out
        (dev,) = machine.hostdevs()  # the NVIDIA card is out, the Intel GPU is in
        assert dev.find("source/address").get("slot") == "0x02"
        assert any("winvm rehearse 0000:00:02.0" in c for c in sh.changes())

        machine.answers(vm())
        code, sh, _ = machine.run("a", "a", "a")
        assert code == 0
        assert machine.hostdevs() == []
        assert not (machine.root / "etc/libvirt/hooks/qemu.d/50-winvm").exists()

    def test_a_failed_rehearsal_leaves_a_working_vm_without_the_gpu(
        self, machine, capsys
    ):
        machine.answers(vm(gpu="nvidia"))
        machine.rehearse = (1, "winvm: rehearsal failed: drivers did not come back")
        code, sh, _ = machine.run("a", "a")
        out = capsys.readouterr().out
        assert code == 1
        assert "Stopped at Stage 3/5  rehearsal" in out
        assert "win11 is defined without the GPU and stays usable over RDP" in out
        assert machine.hostdevs() == []
        assert machine.libvirt.states["win11"] == "running"  # installed, RDP is up
        assert machine.units["winvm-rdp-win11.socket"]["active"]
        events = machine.events()
        assert events[-1]["status"] == "failed"
        status = [e for e in events if e["event"] == "status"][-1]
        assert status["reason"] == "failed"
        ran = [" ".join(c["argv"]) for c in status["commands"]]
        assert any("dumpxml win11" in line for line in ran)
        assert any("journalctl -t winvm" in line for line in ran)
        failed = [e for e in events if e["event"] == "cmd" and e.get("rc") == 1]
        assert "drivers did not come back" in failed[-1]["stdout"]

        machine.rehearse = (0, "")  # fixed: the next run resumes at the rehearsal
        machine.guest = adapters(BASIC, RTX)
        code, sh, prompts = machine.run("a", "a")
        assert code == 0, capsys.readouterr().out
        assert len(machine.hostdevs()) == 2
        assert len(machine.libvirt.did("send-key")) == 10

    def test_a_guest_that_does_not_report_the_card_gets_it_taken_out_again(
        self, machine, capsys
    ):
        machine.answers(vm(gpu="nvidia"))
        machine.guest = adapters(BASIC, ("PCI\\VEN_10DE&DEV_2F04", "43", "NVIDIA GeForce RTX 5070"))
        code, sh, _ = machine.run("a", "a", "a")
        out = capsys.readouterr().out
        assert code == 1
        assert "Stopped at Stage 4/5  gpu" in out
        assert "NVIDIA GeForce RTX 5070 (PCI\\VEN_10DE&DEV_2F04) reports error code 43" in out
        assert "win11 is defined without the GPU and stays usable over RDP" in out
        lv = machine.libvirt
        assert machine.hostdevs() == []
        assert lv.states["win11"] == "running"  # started again, without the card
        mark = ET.fromstring(lv.domains["win11"]).find(f"metadata/{{{DOMAIN_NS}}}recipe")
        assert mark.get("sha256") == ""  # so that the next run tries again

        machine.guest = adapters(BASIC, RTX)
        code, sh, prompts = machine.run("a")
        assert code == 0, capsys.readouterr().out
        assert len(machine.hostdevs()) == 2
        assert not any("rehearse" in c for c in sh.changes())  # that part had passed

    def test_two_vms_with_different_gpus(self, machine, capsys):
        machine.guest = adapters(BASIC, RTX)
        rows = [vm(gpu="nvidia"), vm("cad", rdp_port=3390, guest_ip="192.168.122.61")]
        machine.answers(*rows)
        code, sh, _ = machine.run("a", "a", "a")
        out = capsys.readouterr().out
        assert code == 0, out
        assert len(machine.hostdevs("win11")) == 2
        assert machine.hostdevs("cad") == []
        assert sorted(p.name for p in (machine.root / "etc/winvm").iterdir()) == [
            "cad.conf", "win11.conf",
        ]  # fmt: skip
        assert machine.units["winvm-rdp-cad.socket"]["active"]
        assert "port 3390" in out

    def test_remove_keeps_the_disk_and_the_dependency(self, machine, capsys):
        machine.answers(vm())
        machine.run("a", "a")
        capsys.readouterr()
        code, _, _ = machine.run("a", remove=True)
        assert code == 1  # a running VM is not pulled away
        assert "domain win11 is running; stop it" in capsys.readouterr().out
        machine.libvirt.states["win11"] = "shut off"
        code, sh, _ = machine.run("a", remove=True)
        out = capsys.readouterr().out
        assert code == 0, out
        assert machine.libvirt.domains == {}
        assert "--keep-nvram --keep-tpm" in " ".join(machine.libvirt.did("undefine")[0])
        assert (machine.images / "win11.qcow2").exists()
        assert (machine.images / "win11.qcow2.installed").exists()
        assert not (machine.root / "usr/local/bin/winvm").exists()
        assert not (machine.root / "etc/winvm").exists()
        assert machine.libvirt.hosts == []
        assert not machine.units["winvm-rdp-win11.socket"]["active"]
        assert machine.units["libvirtd"]["active"]
        assert not any("apt-get remove" in c for c in sh.changes())


class TestSingleFile:
    def test_the_built_file_carries_both_recipes(self, tmp_path, sandbox, capsys):
        out = tmp_path / "sux"
        build_single_file.main(out_path=out)
        capsys.readouterr()
        env = os.environ | {
            "HOME": str(sandbox / "home"),
            "XDG_CONFIG_HOME": str(sandbox / "config"),
            "XDG_STATE_HOME": str(sandbox / "state"),
        }
        argv = [sys.executable, str(out), "install"]
        listing = subprocess.run(
            argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False
        )
        assert listing.returncode == 0, listing.stderr
        assert re.search(r"windows-vm\s+not set up\s+Windows 11 VMs", listing.stdout)
        assert re.search(r"^  kvm\s+", listing.stdout, re.MULTILINE)
        asked = subprocess.run(
            [*argv, "windows-vm"], env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        assert asked.returncode == 1, asked.stderr
        assert "windows-vm has questions that are not answered yet:" in asked.stdout
        assert "gpu             GPU passed to this VM" in asked.stdout


class TestLookingGlassBuild:
    STEP = {
        "version": "B8-rc1",
        "commit": "1b485305d9267207701d01adbe7907e90bfd6990",
        "repository": "https://github.com/gnif/LookingGlass.git",
    }

    def test_built_from_the_pinned_commit_into_its_own_prefix(self, hooks, hctx, sh, tmp_path):
        ctx = hctx()
        ctx.cache_dir = tmp_path / "cache"
        step = self.STEP | {"prefix": str(tmp_path / "opt/B8-rc1"), "link": str(tmp_path / "opt/current")}
        probe, actions = hooks["lg_build"](ctx, step, True)
        assert probe.have == "not built"
        lines = [a.shown(sh) for a in actions]
        src = tmp_path / "cache" / "looking-glass-B8-rc1"
        assert lines[0] == (
            "git clone --recursive --branch B8-rc1 "
            f"https://github.com/gnif/LookingGlass.git {src}"
        )
        assert f"-DCMAKE_INSTALL_PREFIX={tmp_path}/opt/B8-rc1" in lines[2]
        assert [line.startswith("sudo") for line in lines] == [False] * 4 + [True, True]
        assert lines[-1] == f"sudo ln -sfn {tmp_path}/opt/B8-rc1 {tmp_path}/opt/current"
        sh.replies["git -C"] = (0, "0000000000000000000000000000000000000000\n")
        with pytest.raises(Fail, match="not the pinned 1b485305"):
            actions[1].fn()
        sh.replies["git -C"] = (0, self.STEP["commit"] + "\n")
        actions[1].fn()
        (tmp_path / "opt/B8-rc1/bin").mkdir(parents=True)
        (tmp_path / "opt/B8-rc1/bin/looking-glass-client").touch()
        (tmp_path / "opt/current").symlink_to(tmp_path / "opt/B8-rc1")
        assert hooks["lg_build"](ctx, step, True).ok is True
        probe, actions = hooks["lg_build"](ctx, step, False)
        assert [a.argv[:2] for a in actions] == [["rm", "-rf"], ["rm", "-f"]]
