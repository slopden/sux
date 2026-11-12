"""The winvm launcher, libvirt hook and rehearsal, on a fake /sys and /proc."""

import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).parents[1]
    / "src/sux/install/recipes/windows-vm/templates/winvm"
)
NVIDIA, NVIDIA_AUDIO, INTEL = "0000:01:00.0", "0000:01:00.1", "0000:00:02.0"
DOMAIN = (
    "<domain><name>win11</name><devices>"
    "<hostdev mode='subsystem' type='pci' managed='yes'><source>"
    "<address domain='0x0000' bus='0x01' slot='0x00' function='0x0'/></source></hostdev>"
    "<hostdev mode='subsystem' type='pci' managed='yes'><source>"
    "<address domain='0x0000' bus='0x01' slot='0x00' function='0x1'/></source></hostdev>"
    "</devices></domain>"
)


class Machine:
    """A fake host: sysfs, procfs, /etc/winvm, and every command that is run."""

    def __init__(self, tmp_path, winvm):
        self.winvm = winvm
        self.sys, self.proc = tmp_path / "sys", tmp_path / "proc"
        self.conf, self.run_dir = tmp_path / "etc", tmp_path / "run"
        self.ran = []
        self.active = {"display-manager.service", "nvidia-persistenced.service"}
        self.states = {}
        self.fail = {}  # first words of a command -> (rc, output)
        self.on_run = None
        for path in (self.conf, self.proc, self.sys / "module"):
            path.mkdir(parents=True)
        (self.sys / "bus/pci/drivers_probe").parent.mkdir(parents=True)
        self.device(NVIDIA, "0x030000", "0x10de", "nvidia", {"DP-5": "connected"}, "card2")
        self.device(NVIDIA_AUDIO, "0x040300", "0x10de", "snd_hda_intel")
        self.device(INTEL, "0x030000", "0x8086", "i915", {"HDMI-A-1": "disconnected"}, "card1")
        info = self.proc / "driver/nvidia/gpus" / NVIDIA
        info.mkdir(parents=True)
        (info / "information").write_text("Model: X\nDevice Minor: \t 0\n")
        for module in ("nvidia", "nvidia_drm", "nvidia_modeset"):
            (self.sys / "module" / module).mkdir()

    def device(self, address, cls, vendor, driver, connectors=None, card=None):  # noqa: PLR0913
        dev = self.sys / "bus/pci/devices" / address
        dev.mkdir(parents=True)
        (dev / "class").write_text(cls)
        (dev / "vendor").write_text(vendor)
        (dev / "reset_method").write_text("flr bus")
        self.bind(address, driver)
        for name, status in (connectors or {}).items():
            node = dev / "drm" / card / f"{card}-{name}"
            node.mkdir(parents=True)
            (node / "status").write_text(status)

    def bind(self, address, driver):
        link = self.sys / "bus/pci/devices" / address / "driver"
        link.unlink(missing_ok=True)
        if driver:
            target = self.sys / "bus/pci/drivers" / driver
            target.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target)

    def plug(self, address, connector, status="connected"):
        (status_file,) = (self.sys / "bus/pci/devices" / address).glob(
            f"drm/*/*-{connector}/status"
        )
        status_file.write_text(status)

    def process(self, pid, name, *nodes):
        fds = self.proc / str(pid) / "fd"
        fds.mkdir(parents=True)
        (self.proc / str(pid) / "comm").write_text(name)
        for n, node in enumerate(nodes):
            (fds / str(n)).symlink_to(node)

    def vm(self, name, **conf):
        conf = {"gpu": "nvidia", "rdp": "true", "rdp_port": "3389", "user": "alice",
                "rdp_host": "box.example.ts.net",
                "nvidia_unload": "nvidia_drm nvidia_modeset nvidia_uvm"} | conf  # fmt: skip
        text = "# managed\n" + "".join(f"{k} = {v}\n" for k, v in conf.items())
        (self.conf / f"{name}.conf").write_text(text)
        self.states.setdefault(name, "shut off")

    def run(self, argv, check=True, timeout=0):  # noqa: ARG002
        self.ran.append(" ".join(argv))
        line = " ".join(a for a in argv if a not in ("-c", "qemu:///system"))
        if self.on_run:
            self.on_run(line)
        for words, reply in self.fail.items():
            if line.startswith(words):
                if check and reply[0]:
                    raise self.winvm.Refused(f"{line} failed ({reply[0]}): {reply[1]}")
                return reply
        tool, verb, last = argv[0], line.split()[1] if " " in line else "", argv[-1]
        if tool == "systemctl" and verb == "is-active":
            return (0 if last in self.active else 3), ""
        if tool == "systemctl" and verb == "stop":
            self.active.discard(last)
        elif tool == "systemctl" and verb == "start":
            self.active.add(last)
        elif tool == "modprobe":
            path = self.sys / "module" / last
            path.rmdir() if "-r" in argv else path.mkdir(exist_ok=True)
        elif tool == "virsh" and verb == "domstate":
            return 0, self.states[last]
        elif tool == "virsh" and verb == "start":
            self.states[last] = "running"
        elif tool == "virsh" and verb == "nodedev-detach":
            self.bind(self.address(last), "vfio-pci")
        elif tool == "virsh" and verb == "nodedev-reattach":
            address = self.address(last)
            self.bind(address, "nvidia" if address == NVIDIA else "snd_hda_intel")
        return 0, ""

    @staticmethod
    def address(node):
        parts = node.removeprefix("pci_").split("_")
        return f"{parts[0]}:{parts[1]}:{parts[2]}.{parts[3]}"

    def hook(self, *args, xml=DOMAIN):
        self.winvm.sys.stdin = io.StringIO(xml)
        return self.winvm.main(["hook", *args])

    def state(self, name="win11"):
        return json.loads((self.run_dir / f"{name}.json").read_text())


@pytest.fixture
def winvm(monkeypatch):
    monkeypatch.setattr("sys.dont_write_bytecode", True)  # not next to the template
    loader = importlib.machinery.SourceFileLoader("winvm", str(SCRIPT))
    spec = importlib.util.spec_from_loader("winvm", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def host(tmp_path, winvm, monkeypatch):
    m = Machine(tmp_path, winvm)
    monkeypatch.setattr(winvm, "SYS", m.sys)
    monkeypatch.setattr(winvm, "PROC", m.proc)
    monkeypatch.setattr(winvm, "CONF", m.conf)
    monkeypatch.setattr(winvm, "RUN", m.run_dir)
    monkeypatch.setattr(winvm, "WAIT_RELEASE_S", 0)
    monkeypatch.setattr(winvm, "run", m.run)
    monkeypatch.setattr(winvm.time, "sleep", lambda _s: None)
    monkeypatch.setattr(winvm.syslog, "syslog", lambda _m: None)
    monkeypatch.setattr(winvm.syslog, "openlog", lambda _n: None)
    monkeypatch.setattr(winvm.os, "geteuid", lambda: 0)
    monkeypatch.setattr(winvm.os, "execvp", lambda *a: m.ran.append("exec " + " ".join(a[1])))
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    m.vm("win11")
    return m


def test_the_script_is_a_template_without_placeholders():
    assert "$" not in SCRIPT.read_text()  # string.Template would trip over one


# ---------------------------------------------------------------- the hook


class TestHook:
    def test_other_peoples_guests_and_guests_without_devices_are_left_alone(self, host):
        assert host.hook("someone-else", "prepare", "begin", "-") == 0
        assert host.hook("win11", "prepare", "begin", "-", xml="<domain><devices/></domain>") == 0
        assert host.hook("win11", "started", "begin", "-") == 0
        assert host.ran == []

    def test_headless_the_display_manager_is_stopped_and_comes_back(self, host, capsys):
        assert host.hook("win11", "prepare", "begin", "-") == 0
        assert host.ran == [
            "systemctl is-active --quiet display-manager.service",
            "systemctl stop display-manager.service",
            "systemctl is-active --quiet nvidia-persistenced.service",
            "systemctl stop nvidia-persistenced.service",
            "systemctl is-active --quiet nvidia-powerd.service",
            "modprobe -r nvidia_drm",
            "modprobe -r nvidia_modeset",
        ]  # nvidia_uvm is not loaded, and never a virsh call from inside libvirt
        assert "stopping display-manager.service" in capsys.readouterr().err
        state = host.state()
        assert state["display_manager"] is True
        assert state["drivers"] == {NVIDIA: "nvidia", NVIDIA_AUDIO: "snd_hda_intel"}
        host.ran.clear()
        assert host.hook("win11", "release", "end", "shutdown") == 0
        assert host.ran == [
            "modprobe nvidia_modeset",
            "modprobe nvidia_drm",
            "systemctl start --no-block nvidia-persistenced.service",
            "systemctl start --no-block display-manager.service",
        ]
        assert not (host.run_dir / "win11.json").exists()
        assert host.hook("win11", "release", "end", "shutdown") == 0  # nothing left

    def test_with_a_display_on_the_intel_output_the_desktop_stays_up(self, host):
        host.plug(INTEL, "HDMI-A-1")
        assert host.hook("win11", "prepare", "begin", "-") == 0
        assert not any("display-manager" in c for c in host.ran)
        assert host.state()["display_manager"] is False

    def test_refuses_and_lists_the_processes_that_hold_the_card(self, host, capsys):
        host.plug(INTEL, "HDMI-A-1")
        host.process(4242, "python3", "/dev/nvidia0", "/dev/null")
        host.process(4343, "gnome-shell", "/dev/dri/card2")
        host.process(4444, "bash", "/dev/dri/card1")  # the Intel GPU: not ours
        assert host.hook("win11", "prepare", "begin", "-") == 1
        err = capsys.readouterr().err
        assert "win11 is not started: 0000:01:00.0 is in use on the host by:" in err
        assert "pid 4242  python3  (/dev/nvidia0)" in err
        assert "pid 4343  gnome-shell  (/dev/dri/card2)" in err
        assert "4444" not in err
        assert not any(c.startswith("modprobe") for c in host.ran)
        # what was stopped for the attempt is started again
        assert host.ran[-1] == "systemctl start --no-block nvidia-persistenced.service"
        assert not (host.run_dir / "win11.json").exists()

    def test_headless_a_compute_job_still_refuses_and_the_desktop_returns(
        self, host, capsys
    ):
        host.process(4242, "python3", "/dev/nvidia0")
        assert host.hook("win11", "prepare", "begin", "-") == 1
        assert "pid 4242  python3" in capsys.readouterr().err
        assert host.ran[-1] == "systemctl start --no-block display-manager.service"
        assert "display-manager.service" in host.active

    def test_a_module_that_will_not_unload_puts_everything_back(self, host):
        host.fail["modprobe -r nvidia_modeset"] = (1, "Module nvidia_modeset is in use")
        assert host.hook("win11", "prepare", "begin", "-") == 1
        assert host.ran[-3:] == [
            "modprobe nvidia_drm",
            "systemctl start --no-block nvidia-persistenced.service",
            "systemctl start --no-block display-manager.service",
        ]

    def test_a_failed_start_restores_the_binding(self, host):
        host.hook("win11", "prepare", "begin", "-")
        host.bind(NVIDIA, "")  # libvirt took it back from vfio-pci, nothing bound
        host.ran.clear()
        assert host.hook("win11", "release", "end", "failed") == 0
        assert (host.sys / "bus/pci/drivers_probe").read_text() == NVIDIA
        assert "systemctl start --no-block display-manager.service" in host.ran

    def test_a_card_another_guest_has_is_refused(self, host, capsys):
        host.bind(NVIDIA, "vfio-pci")
        assert host.hook("win11", "prepare", "begin", "-") == 1
        assert "already handed to a guest" in capsys.readouterr().err

    def test_reset_method_is_written_when_asked_for(self, host):
        host.vm("win11", reset_method="bus")
        host.hook("win11", "prepare", "begin", "-")
        assert (host.sys / "bus/pci/devices" / NVIDIA / "reset_method").read_text() == "bus"

    def test_the_intel_gpu_never_touches_the_desktop(self, host, capsys):
        xml = DOMAIN.replace("bus='0x01' slot='0x00' function='0x0'", "bus='0x00' slot='0x02' function='0x0'")
        xml = xml.replace("bus='0x01' slot='0x00' function='0x1'", "bus='0x00' slot='0x02' function='0x0'")
        assert host.winvm.hostdevs(xml) == [INTEL, INTEL]
        assert host.hook("win11", "prepare", "begin", "-", xml=xml) == 0
        assert host.ran == []
        host.plug(INTEL, "HDMI-A-1")
        assert host.hook("win11", "prepare", "begin", "-", xml=xml) == 1
        assert "a display is connected to 0000:00:02.0 (HDMI-A-1)" in capsys.readouterr().err


class TestRehearsal:
    def test_detach_reattach_and_the_drivers_are_back(self, host, capsys):
        code = host.winvm.main(["rehearse", NVIDIA, NVIDIA_AUDIO, "--unload", "nvidia_drm"])
        assert code == 0
        virsh = [c.split("system ")[1] for c in host.ran if c.startswith("virsh")]
        assert virsh == [
            "nodedev-detach pci_0000_01_00_0",
            "nodedev-detach pci_0000_01_00_1",
            "nodedev-reattach pci_0000_01_00_0",
            "nodedev-reattach pci_0000_01_00_1",
        ]
        assert host.ran[-1] == "nvidia-smi"
        assert "display-manager.service" in host.active
        assert "rehearsal passed" in capsys.readouterr().err

    def test_a_failed_detach_reattaches_and_restarts_the_desktop(self, host, capsys):
        host.fail["virsh nodedev-detach pci_0000_01_00_1"] = (1, "device busy")
        assert host.winvm.main(["rehearse", NVIDIA, NVIDIA_AUDIO]) == 1
        assert "rehearsal failed" in capsys.readouterr().err
        assert host.winvm.driver(NVIDIA) == "nvidia"
        assert "display-manager.service" in host.active
        assert "nvidia-smi" not in host.ran

    def test_a_driver_that_does_not_come_back_fails_it(self, host, capsys):
        def lose(line):
            if line.startswith("virsh nodedev-reattach pci_0000_01_00_0"):
                host.fail[line] = (0, "")
                host.bind(NVIDIA, "")

        host.on_run = lose
        assert host.winvm.main(["rehearse", NVIDIA, NVIDIA_AUDIO]) == 1
        assert "drivers did not come back" in capsys.readouterr().err

    def test_needs_root(self, host, monkeypatch, capsys):
        monkeypatch.setattr(host.winvm.os, "geteuid", lambda: 1000)
        assert host.winvm.main(["rehearse", NVIDIA]) == 1
        assert "needs root" in capsys.readouterr().err


# ---------------------------------------------------------------- the launcher


class TestLauncher:
    def test_usage(self, host, capsys):
        assert host.winvm.main([]) == 2
        assert host.winvm.main(["explode"]) == 2
        out = capsys.readouterr().out
        assert "winvm start [name]" in out
        assert "hook" not in out.split("With exactly one")[0]

    def test_the_name_may_be_left_out_with_exactly_one_vm(self, host, capsys):
        assert host.winvm.main(["start"]) == 0
        out = capsys.readouterr().out
        assert "win11 started" in out
        assert "RDP: box.example.ts.net:3389  (user alice)" in out
        host.vm("cad", gpu="none", rdp_port="3390")
        assert host.winvm.main(["start"]) == 1
        assert "which VM? (cad, win11)" in capsys.readouterr().err
        assert host.winvm.main(["stop", "nope"]) == 1
        assert "no VM named nope (there is: cad, win11)" in capsys.readouterr().err

    def test_start_warns_before_the_desktop_goes(self, host, capsys):
        host.winvm.main(["start", "win11"])
        out = capsys.readouterr().out
        assert out.index("WARNING: no display on another GPU") < out.index("win11 started")
        host.states["win11"] = "shut off"
        host.plug(INTEL, "HDMI-A-1")
        host.winvm.main(["start", "win11"])
        assert "WARNING" not in capsys.readouterr().out

    def test_a_gpu_another_vm_is_using_is_refused(self, host, capsys):
        host.vm("cad", rdp_port="3390")
        host.states["cad"] = "running"
        assert host.winvm.main(["start", "win11"]) == 1
        err = capsys.readouterr().err
        assert "the nvidia GPU is in use by cad; stop it first: winvm stop cad" in err
        assert not any("virsh" in c and " start " in c for c in host.ran)
        host.vm("plain", gpu="none")
        assert host.winvm.main(["start", "plain"]) == 0  # no GPU: nothing to share

    def test_a_gpu_that_is_not_there_is_refused(self, host, capsys):
        host.vm("win11", gpu="igpu")
        (host.sys / "bus/pci/devices" / INTEL / "class").write_text("0x060000")
        assert host.winvm.main(["start"]) == 1
        assert "gpu = igpu, which is not here" in capsys.readouterr().err

    def test_a_failed_start_shows_what_the_hook_said(self, host, capsys):
        host.fail["virsh start win11"] = (1, "Hook script execution failed: in use by pid 7")
        assert host.winvm.main(["start"]) == 1
        err = capsys.readouterr().err
        assert "win11 did not start" in err
        assert "in use by pid 7" in err
        assert "journalctl -t winvm" in err

    def test_stop_status_and_list(self, host, capsys):
        host.vm("cad", gpu="none", rdp="false")
        host.states["win11"] = "running"
        assert host.winvm.main(["list"]) == 0
        assert capsys.readouterr().out == "cad\nwin11\n"
        assert host.winvm.main(["status"]) == 0
        out = capsys.readouterr().out
        assert "cad: shut off, gpu none\n  RDP forward: off" in out
        assert "win11: running, gpu nvidia\n  RDP: box.example.ts.net:3389" in out
        assert host.winvm.main(["status", "cad"]) == 0
        assert "win11" not in capsys.readouterr().out
        assert host.winvm.main(["stop", "win11"]) == 0
        assert any(c.endswith("shutdown win11") for c in host.ran)
        assert host.winvm.main(["stop", "cad"]) == 0
        assert "cad is not running" in capsys.readouterr().out

    def test_looking_glass_only_with_the_desktop_on_the_intel_output(
        self, host, monkeypatch, tmp_path
    ):
        shm = tmp_path / "shm"
        shm.touch()
        host.vm("win11", looking_glass="true", fullscreen="true", shm=str(shm), client="lg")
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        host.winvm.main(["start"])
        assert not any(c.startswith("exec") for c in host.ran)  # headless: no window
        host.states["win11"] = "shut off"
        host.plug(INTEL, "HDMI-A-1")
        host.winvm.main(["start"])
        assert host.ran[-1] == f"exec lg -f {shm} win:fullScreen=yes"

    def test_udev_helper(self, host):
        assert host.winvm.main(["display-elsewhere", NVIDIA]) == 1
        assert host.winvm.main(["display-elsewhere", INTEL]) == 0
        host.plug(INTEL, "HDMI-A-1")
        assert host.winvm.main(["display-elsewhere", NVIDIA]) == 0
