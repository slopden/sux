"""The libvirt and disk image step kinds, against a fake libvirt."""

import os
import time
import xml.etree.ElementTree as ET

import pytest

from sux.install.facts import FACTS, Facts
from sux.install import facts as facts_mod
from sux.install.log import Fail
from sux.install.resources import (
    DOMAIN_NS,
    SECRET_MARK,
    ImageRes,
    LibvirtDhcpHostRes,
    LibvirtDomainRes,
    LibvirtNetworkRes,
    SecretRes,
)

from .test_install_resources import apply, plan, shown

pytestmark = pytest.mark.usefixtures("sandbox")

XML = "<domain type='kvm'><name>vm1</name><memory unit='GiB'>{}</memory></domain>"


def domain(memory=8, present=True, **more):
    return LibvirtDomainRes({"name": "vm1", "content": XML.format(memory)} | more, present)


class TestDomain:
    def test_defined_once_and_then_left_alone(self, ctx, sh, libvirt):
        res = domain()
        p, actions = plan(res, ctx)
        assert (p.ok, p.have) == (False, "not defined")
        assert shown(actions, sh) == ["sudo virsh -c qemu:///system define /dev/stdin  < content"]
        apply(actions, sh)
        root = ET.fromstring(libvirt.domains["vm1"])
        assert root.find(f"metadata/{{{DOMAIN_NS}}}recipe").get("sha256") == res.sha256
        p, actions = plan(domain(), ctx)
        assert (p.ok, p.have, actions) == (True, "defined (shut off)", [])
        assert len(libvirt.did("define")) == 1

    def test_probing_needs_neither_sudo_nor_the_libvirt_group(self, ctx, sh, libvirt):
        apply(plan(domain(), ctx)[1], sh)
        sh.spawned.clear()
        domain().probe(ctx)
        assert all(argv[:2] == ["virsh", "-r"] for argv in sh.spawned)

    def test_what_libvirt_adds_is_not_a_difference(self, ctx, sh, libvirt):
        apply(plan(domain(), ctx)[1], sh)
        root = ET.fromstring(libvirt.domains["vm1"])
        ET.SubElement(root, "devices").append(ET.Element("controller", type="usb"))
        libvirt.domains["vm1"] = ET.tostring(root, encoding="unicode")
        assert domain().probe(ctx).ok is True

    def test_a_changed_rendering_is_defined_again_with_the_same_uuid(
        self, ctx, sh, libvirt
    ):
        apply(plan(domain(), ctx)[1], sh)
        libvirt.states["vm1"] = "running"  # takes effect at its next start
        res = domain(memory=16)
        p, actions = plan(res, ctx)
        assert (p.ok, p.reason) == (False, "definition differs")
        assert [a.desc for a in actions] == [
            "back up the definition of vm1",
            "define domain vm1",
        ]
        assert str(ctx.backup_dir / "domain-vm1.xml") in shown(actions, sh)[0]
        apply(actions, sh)
        root = ET.fromstring(libvirt.domains["vm1"])
        assert root.findtext("uuid") == "uuid-of-vm1"
        assert root.findtext("memory") == "16"
        assert (ctx.backup_dir / "domain-vm1.xml").read_text().count("<memory") == 1
        assert domain(memory=16).probe(ctx).ok is True

    def test_no_libvirt_yet_reads_as_not_defined(self, ctx, sh):
        sh.replies["virsh"] = (127, "")
        assert domain().probe(ctx).have == "not defined"
        assert domain(present=False).probe(ctx).ok is True

    def test_xml_that_does_not_parse_is_a_clear_failure(self, ctx, libvirt):
        res = LibvirtDomainRes({"name": "vm1", "content": "<domain>"})
        with pytest.raises(Fail, match="domain vm1: its XML does not parse"):
            plan(res, ctx)

    def test_removal_undefines_and_keeps_nvram_and_tpm(self, ctx, sh, libvirt):
        apply(plan(domain(), ctx)[1], sh)
        p, actions = plan(domain(present=False), ctx)
        assert p.ok is False
        assert shown(actions, sh)[-1] == (
            "sudo virsh -c qemu:///system undefine vm1 --keep-nvram --keep-tpm "
            "--managed-save --snapshots-metadata --checkpoints-metadata"
        )
        apply(actions, sh)
        assert libvirt.domains == {}
        assert domain(present=False).probe(ctx).ok is True

    def test_purge_also_deletes_nvram_and_tpm_state(self, ctx, sh, libvirt):
        apply(plan(domain(), ctx)[1], sh)
        _, actions = plan(domain(present=False, purge=True), ctx)
        assert "undefine vm1 --nvram --tpm " in shown(actions, sh)[-1]

    def test_a_running_domain_is_not_removed(self, ctx, sh, libvirt):
        apply(plan(domain(), ctx)[1], sh)
        libvirt.states["vm1"] = "running"
        with pytest.raises(Fail, match="domain vm1 is running; stop it: virsh .* shutdown vm1"):
            domain(present=False).probe(ctx)


class TestNetwork:
    def test_started_and_autostarted(self, ctx, sh, libvirt):
        assert LibvirtNetworkRes({}).probe(ctx).ok is True
        libvirt.active = libvirt.autostart = False
        res = LibvirtNetworkRes({"name": "default"})
        p, actions = plan(res, ctx)
        assert (p.ok, p.have) == (False, "not active, not autostart")
        apply(actions, sh)
        assert [c[0] for c in libvirt.calls[-2:]] == ["net-autostart", "net-start"]
        assert res.probe(ctx).ok is True

    def test_before_libvirt_is_installed_and_never_removed(self, ctx, sh):
        sh.replies["virsh"] = (127, "")
        p, actions = plan(LibvirtNetworkRes({}), ctx)
        assert p.have == "not there (yet)"
        assert actions[-1].check is False  # its package may have started it by then
        assert LibvirtNetworkRes({}, present=False).probe(ctx).ok is True


class TestDhcpHost:
    STEP = {"mac": "52:54:00:AA:BB:CC", "ip": "192.168.122.50", "name": "vm1"}

    def test_reserved_without_restarting_the_network(self, ctx, sh, libvirt):
        res = LibvirtDhcpHostRes(self.STEP)
        p, actions = plan(res, ctx)
        assert (p.ok, p.have) == (False, "not reserved")
        apply(actions, sh)
        assert libvirt.did("net-update") == [
            ["net-update", "default", "add", "ip-dhcp-host",
             "<host mac='52:54:00:aa:bb:cc' name='vm1' ip='192.168.122.50'/>",
             "--live", "--config"]
        ]  # fmt: skip
        assert res.probe(ctx).ok is True

    def test_an_address_change_replaces_the_reservation(self, ctx, sh, libvirt):
        apply(plan(LibvirtDhcpHostRes(self.STEP), ctx)[1], sh)
        moved = LibvirtDhcpHostRes(self.STEP | {"ip": "192.168.122.51"})
        _, actions = plan(moved, ctx)
        assert [a.desc.split()[0] for a in actions] == ["delete", "add"]
        apply(actions, sh)
        assert len(libvirt.hosts) == 1
        assert "192.168.122.51" in libvirt.hosts[0]

    def test_wrong_subnet_or_taken_address_stops_the_run(self, ctx, sh, libvirt):
        with pytest.raises(Fail, match=r"10.0.0.5 is not on network default \(192.168.122.0/24\)"):
            LibvirtDhcpHostRes(self.STEP | {"ip": "10.0.0.5"}).probe(ctx)
        libvirt.hosts.append("<host mac='52:54:00:00:00:01' ip='192.168.122.50'/>")
        with pytest.raises(Fail, match="192.168.122.50 is reserved for 52:54:00:00:00:01"):
            LibvirtDhcpHostRes(self.STEP).probe(ctx)

    def test_inactive_network_and_removal(self, ctx, sh, libvirt):
        libvirt.active = False
        _, actions = plan(LibvirtDhcpHostRes(self.STEP), ctx)
        assert "--live" not in actions[0].argv
        apply(actions, sh)
        gone = LibvirtDhcpHostRes(self.STEP, present=False)
        p, actions = plan(gone, ctx)
        assert p.ok is False
        apply(actions, sh)
        assert libvirt.hosts == []
        assert gone.probe(ctx).ok is True

    def test_no_network_yet(self, ctx, sh):
        sh.replies["virsh"] = (127, "")
        p, actions = plan(LibvirtDhcpHostRes(self.STEP), ctx)
        assert p.have == "no network default (yet)"
        assert "--live" in actions[0].argv


class TestImage:
    def test_a_disk_is_created_once_and_never_touched_again(self, ctx, sh, tmp_path, me):
        step = {"path": str(tmp_path / "vm.qcow2"), "size": "120G"} | me
        res = ImageRes(step)
        p, actions = plan(res, ctx)
        assert p.have == "missing"
        assert shown(actions, sh)[0] == f"sudo qemu-img create -f qcow2 {res.path} 120G"
        res.path.write_text("windows")  # the fake shell has no qemu-img
        res.path.chmod(0o644)  # libvirt changes owner and mode while it runs
        assert ImageRes(step | {"size": "200G"}).probe(ctx).ok is True

    def test_a_disk_is_removed_only_with_purge(self, ctx, sh, tmp_path, me):
        step = {"path": str(tmp_path / "vm.qcow2"), "size": "1G"} | me
        (tmp_path / "vm.qcow2").write_text("windows")
        kept = ImageRes(step, present=False)
        p, actions = plan(kept, ctx)
        assert (p.ok, actions) == (True, [])
        assert "kept" in p.have
        purged = ImageRes(step | {"purge": True}, present=False)
        p, actions = plan(purged, ctx)
        assert p.ok is False
        apply(actions, sh)
        assert not purged.path.exists()

    def test_an_iso_is_rebuilt_when_its_files_change(self, ctx, sh, tmp_path, me):
        source = tmp_path / "files"
        source.mkdir()
        (source / "a.txt").write_text("a")
        step = {"path": str(tmp_path / "x.iso"), "source": str(source), "label": "X"} | me
        res = ImageRes(step)
        _, actions = plan(res, ctx)
        assert shown(actions, sh)[0] == (
            f"sudo xorriso -as mkisofs -quiet -J -r -V X -o {res.path} {source}"
        )
        assert res.label() == f"image {res.path}"
        res.path.write_text("iso")
        os.utime(res.path, (time.time() + 5, time.time() + 5))
        assert res.probe(ctx).ok is True
        os.utime(source / "a.txt", (time.time() + 9, time.time() + 9))
        assert res.probe(ctx).reason == "older than a.txt"
        os.utime(res.path, (time.time() + 20, time.time() + 20))
        ctx.changing.add(f"file:{source}/b.txt")
        assert res.probe(ctx).reason == "its files change in this run"
        gone = ImageRes(step, present=False)  # derived from the files: just removed
        assert plan(gone, ctx)[0].ok is False

    def test_size_or_source(self, tmp_path):
        with pytest.raises(Fail, match="give `size` .* or `source`"):
            ImageRes({"path": str(tmp_path / "x")})


class TestSecretInSeveralPlaces:
    def test_every_place_gets_the_same_secret_which_is_then_kept(self, tmp_path, me, ctx, sh):
        content = f"<a>{SECRET_MARK}</a>\n<b>{SECRET_MARK}</b>\n"
        res = SecretRes({"path": str(tmp_path / "x.xml"), "content": content, "bytes": 9} | me)
        apply(plan(res, ctx)[1], sh)
        first = res.path.read_text()
        a, b = first.split("</a>\n<b>")
        assert a.removeprefix("<a>") == b.removesuffix("</b>\n")
        assert len(a.removeprefix("<a>")) == 12
        assert res.probe(ctx).ok is True
        res.path.write_text(first.replace("</b>", "</b> "))  # the rest changed
        res.path.chmod(0o600)
        p, actions = plan(res, ctx)
        assert "keeps the existing secret" in actions[-1].desc
        assert actions[-1].stdin == first


class TestFacts:
    def test_what_a_gpu_handoff_depends_on(self, sh, tmp_path, monkeypatch):
        monkeypatch.setattr(facts_mod, "SYSFS", tmp_path)
        devices = tmp_path / "bus" / "pci" / "devices"
        group = tmp_path / "kernel" / "iommu_groups" / "12"
        for address, cls in [("0000:01:00.0", "0x030000"), ("0000:01:00.1", "0x040300")]:
            dev = devices / address
            (dev / "drm" / "card2" / "card2-DP-5").mkdir(parents=True)
            (dev / "drm" / "renderD129").mkdir()
            (dev / "drm" / "card2" / "card2-DP-5" / "status").write_text("connected\n")
            (dev / "class").write_text(cls)
            (dev / "vendor").write_text("0x10de")
            (dev / "device").write_text("0x2f04")
            (group / "devices" / address).mkdir(parents=True)
            os.symlink(group, dev / "iommu_group")
        (gpu,) = Facts(sh).get("gpu_devices")
        assert gpu["functions"] == gpu["group_devices"] == ["0000:01:00.0", "0000:01:00.1"]
        assert gpu["connectors"] == {"DP-5": "connected"}
        assert (gpu["device"], gpu["drm"]) == ("0x2f04", ["card2", "renderD129"])

    def test_performance_cpus_and_memory(self, sh, tmp_path, monkeypatch):
        monkeypatch.setattr(facts_mod, "SYSFS", tmp_path)
        monkeypatch.setattr(facts_mod, "PROC", tmp_path)
        (tmp_path / "devices" / "system" / "cpu").mkdir(parents=True)
        (tmp_path / "devices" / "system" / "cpu" / "online").write_text("0-7\n")
        (tmp_path / "meminfo").write_text("MemTotal:       65700000 kB\n")
        (tmp_path / "cpuinfo").write_text("flags\t\t: fpu vmx sse\n")
        assert FACTS["performance_cpus"](sh) == list(range(8))
        (tmp_path / "devices" / "cpu_core").mkdir()
        (tmp_path / "devices" / "cpu_core" / "cpus").write_text("0-3,6\n")
        assert FACTS["performance_cpus"](sh) == [0, 1, 2, 3, 6]  # hybrid: P-cores
        assert FACTS["memory_gib"](sh) == 62
        assert FACTS["virtualization"](sh) == "vmx"
