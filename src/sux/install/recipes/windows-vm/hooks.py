"""Hooks for windows-vm: what is computed, waited for, or asked of a guest.

Values the recipe cannot write as data (which GPUs can be passed on, CPU
pinning, defaults that depend on the host), the steps that take time and can
fail half way (installing Windows, the GPU handoff rehearsal, the first boot
with a GPU), and the health checks that talk to the guest. The engine provides
Action, Probe, Fail, say, read_file and what say() takes (Note, Check, Ask)
as globals.

A check that would stop a later stage does not stop the whole plan: it is
planned as a BLOCKED action, which fails when (and only when) its stage is
reached, so the stages before it still apply.
"""

import base64
import grp
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import time
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

VIRSH = ["virsh", "-c", "qemu:///system"]
VIRSH_RO = ["virsh", "-r", "-c", "qemu:///system"]
WINVM = "/usr/local/bin/winvm"
STATE = Path("/var/lib/winvm")
PROC = Path("/proc")
SUX_NS = "urn:sux:install"  # where the engine records what it defined
VM_NAME = r"[A-Za-z][A-Za-z0-9-]{0,14}"  # also the Windows computer name
USER_NAME = r"[A-Za-z][A-Za-z0-9_.-]{0,19}"
VFIO = "vfio-pci"
DISPLAY_MANAGER = "display-manager.service"
GUEST_VENDOR = {"nvidia": "VEN_10DE", "igpu": "VEN_8086"}
GIB = 1 << 30
POLL_S = 15
SHUTDOWN_S = 180
AGENT_S = 600
# RDP connection request (X.224 over TPKT) asking for TLS or CredSSP: any RDP
# server answers it with a packet that starts with the TPKT version, 3.
RDP_HELLO = bytes.fromhex("030000130ee000000000000100080003000000")
TPKT = 3
_sleep = time.sleep
_clock = time.monotonic


# ---------------------------------------------------------------- variables


def _candidates(values):
    """GPUs that can be handed to a guest, by the name the answers use.

    nvidia: a discrete NVIDIA card whose IOMMU group holds only its own
    functions. igpu: an Intel integrated GPU alone in its group.
    """
    found = {}
    for gpu in values["gpu_devices"]:
        clean = gpu["group_devices"] and set(gpu["group_devices"]) <= set(
            gpu["functions"]
        )
        kind = {"nvidia": "nvidia", "intel": "igpu"}.get(gpu["vendor"])
        if kind and clean and kind not in found:
            found[kind] = gpu
    return found


def gpu_choices(values):
    return ["none", *_candidates(values)]


def desktop_elsewhere(values):
    """Whether a display is connected to a GPU other than the NVIDIA card.

    Then the host desktop can stay up while a guest has that card.
    """
    card = _candidates(values).get("nvidia")
    others = [g for g in values["gpu_devices"] if card and g is not card]
    return any("connected" in g["connectors"].values() for g in others)


def default_vcpus(values):
    """All performance-core threads but four, in whole cores (at most 12)."""
    spare = len(values["performance_cpus"]) - 4
    return max(2, min(12, spare - spare % 2))


def default_memory(values):
    return max(4, min(32, values["memory_gib"] // 2))


def default_guest_ip(values):
    """An address on libvirt's default network, picked from the VM's name."""
    r = values.facts.sh.probe([*VIRSH_RO, "net-dumpxml", "default"])
    found = re.search(r"<ip address='([\d.]+)'", r.stdout if r.returncode == 0 else "")
    base = (found[1] if found else "192.168.122.1").rsplit(".", 1)[0]
    return f"{base}.{50 + zlib.crc32(str(values['name']).encode()) % 150}"


def vm_mac(values):
    """A stable MAC in QEMU's range, from the VM's name."""
    digest = hashlib.sha256(str(values["name"]).encode()).hexdigest()
    return "52:54:00:" + ":".join(digest[i : i + 2] for i in (0, 2, 4))


def vm_installed(values):
    """Windows was installed on this VM's disk (the marker sits next to it)."""
    return Path(f"{values['disk']}.installed").exists()


def _vcpus(values):
    text = str(values["vcpus"])
    return int(text) if text.isdigit() else 0


def vm_pins(values):
    """vCPU -> host CPU: the last performance-core threads, one each."""
    cpus = values["performance_cpus"][-_vcpus(values) :] if _vcpus(values) else []
    return [{"vcpu": n, "cpu": cpu} for n, cpu in enumerate(cpus)]


def vm_cores(values):
    return max(1, _vcpus(values) // 2)


def vm_emulator_cpus(values):
    """Where QEMU's own threads run: the performance CPUs no vCPU is pinned to."""
    pinned = {pin["cpu"] for pin in vm_pins(values)}
    free = [c for c in values["performance_cpus"] if c not in pinned]
    return ",".join(str(c) for c in free or values["performance_cpus"])


def vm_gpu_address(values):
    gpu = _candidates(values).get(values["gpu"])
    return gpu["address"] if gpu else ""


def vm_gpu_functions(values):
    """Every function of the VM's GPU, as the parts of a libvirt PCI address."""
    gpu = _candidates(values).get(values["gpu"])
    out = []
    for address in gpu["functions"] if gpu else []:
        domain, bus, rest = address.split(":")
        slot, function = rest.split(".")
        parts = {"domain": domain, "bus": bus, "slot": slot, "function": function}
        out.append(parts | {"pci": address})
    return out


# ---------------------------------------------------------------- helpers


def _blocked(have, fix):
    """A check that fails its stage when reached, and is shown in the plan."""

    def stop():
        raise Fail(fix)

    return Probe(False, f"BLOCKED: {have}"), [Action(f"BLOCKED: {fix}", fn=stop)]


def _state(ctx, name):
    """libvirt's state of a domain ("running", "shut off"), "" if undefined."""
    r = ctx.sh.probe([*VIRSH_RO, "domstate", name])
    return r.stdout.strip() if r.returncode == 0 else ""


def _manage(ctx, *args):
    """A virsh call that needs write access: as this user if it can, else sudo.

    Returns the finished process, or None when neither is possible yet.
    """
    groups = {grp.getgrgid(g).gr_name for g in os.getgroups()}
    member = "libvirt" in groups or os.geteuid() == 0
    return ctx.sh.probe([*VIRSH, *args], sudo=not member)


def _agent(ctx, name, execute, **arguments):
    """One guest agent command; its "return", or None if there is no answer."""
    command = {"execute": execute} | ({"arguments": arguments} if arguments else {})
    r = _manage(ctx, "qemu-agent-command", name, json.dumps(command))
    if r is None or r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout).get("return", {})
    except ValueError:
        return None


def _guest_run(ctx, name, path, *args):
    """Run a program in the guest through its agent; its output, or None."""
    started = _agent(
        ctx,
        name,
        "guest-exec",
        **{"path": path, "arg": list(args)} | {"capture-output": True},
    )
    if not started or "pid" not in started:
        return None
    deadline = _clock() + 60
    while _clock() < deadline:
        status = _agent(ctx, name, "guest-exec-status", pid=started["pid"])
        if status and status.get("exited"):
            return base64.b64decode(status.get("out-data", "")).decode(errors="replace")
        _sleep(1)
    return None


def _wait(ctx, what, done, seconds):
    """Poll `done()` until it is true; Fail after `seconds`."""
    deadline, started = _clock() + seconds, _clock()
    while not done():
        if _clock() > deadline:
            raise Fail(f"{what}: no result after {seconds // 60} minutes")
        _sleep(POLL_S)
        ctx.log.event("waiting", what=what, seconds=round(_clock() - started))
    return round(_clock() - started)


def _power_off(ctx, name):
    """Shut the guest down; pull the plug if it does not go."""
    if _state(ctx, name) in ("", "shut off"):
        return
    ctx.sh.run([*VIRSH, "shutdown", name], check=False)
    try:
        _wait(
            ctx,
            f"{name} shutting down",
            lambda: _state(ctx, name) == "shut off",
            SHUTDOWN_S,
        )
    except Fail:
        ctx.sh.run([*VIRSH, "destroy", name], check=False)


def _strip_gpu(ctx, name):
    """Take every passed-through device out of a domain's definition.

    Used when a GPU stage fails: the VM stays usable without the GPU. The
    recorded hash is cleared with it, so the next run defines the GPU again.
    """
    r = ctx.sh.probe(
        [*VIRSH, "dumpxml", "--inactive", "--security-info", name], sudo=True
    )
    if r is None or r.returncode != 0:
        return False
    root = ET.fromstring(r.stdout)
    devices = root.find("devices")
    passed = devices.findall("hostdev") if devices is not None else []
    for dev in passed:
        devices.remove(dev)
    for mark in root.iterfind(f"metadata/{{{SUX_NS}}}recipe"):
        mark.set("sha256", "")
    if passed:
        say(Note(f"defining {name} without its GPU, so that it stays usable", 2))
        xml = ET.tostring(root, encoding="unicode")
        ctx.sh.run([*VIRSH, "define", "/dev/stdin"], stdin=xml, check=False)
    return bool(passed)


def _without_gpu(name):
    return (
        f"{name} is defined without the GPU and stays usable over RDP "
        f"(winvm start {name}). Fix the cause and run this again, or set its "
        "gpu to none with --reconfigure."
    )


def _driver(address):
    link = Path("/sys/bus/pci/devices") / address / "driver"
    return link.resolve().name if link.exists() else ""


def _holders(gpu):
    """Processes this user can see that hold the GPU's device nodes open."""
    nodes = {f"/dev/dri/{name}" for name in gpu["drm"]}
    found = []
    for proc in PROC.glob("[0-9]*"):
        try:
            held = {str(fd.readlink()) for fd in (proc / "fd").iterdir()} & nodes
        except OSError:
            continue
        if held:
            name = (proc / "comm").read_text().strip()
            found.append(f"{name} (pid {proc.name})")
    return sorted(found)


def _stops_desktop(ctx, kind):
    """Whether starting a guest with this GPU stops the host's desktop."""
    if kind != "nvidia" or desktop_elsewhere(ctx.values):
        return False
    return ctx.sh.out(["systemctl", "is-active", DISPLAY_MANAGER]) == "active"


def _in_desktop(environ=None):
    """Whether this run would die with the desktop (not SSH, tmux or a console)."""
    env = os.environ if environ is None else environ
    graphical = env.get("XDG_SESSION_TYPE") in ("wayland", "x11")
    return graphical and not (env.get("SSH_CONNECTION") or env.get("TMUX"))


def _gpu_blocker(ctx, step):  # noqa: PLR0911
    """Why this VM's GPU cannot be handed over right now: (have, fix) or None."""
    kind, name = step["gpu"], step["vm"]
    gpu = _candidates(ctx.values).get(kind)
    if gpu is None:
        return f"no {kind} GPU that can be passed on", (
            f"{name}: this machine has no {kind} GPU in an IOMMU group of its own; "
            "set gpu to none (--reconfigure)"
        )
    if _driver(gpu["address"]) == VFIO:
        return "in use by a running guest", (
            f"{name}: the {kind} GPU is in a running guest; stop that one first"
        )
    connected = [c for c, s in gpu["connectors"].items() if s == "connected"]
    if kind == "igpu" and connected:
        return f"a display is connected to it ({', '.join(connected)})", (
            f"{name}: the Intel GPU has a display on {', '.join(connected)}; it can "
            "only be passed on while the host does not use it"
        )
    rom = step.get("igd_rom") or ""
    if kind == "igpu" and not Path(rom).is_file():
        return f"no option ROM at {rom or '(not answered)'}", (
            f"{name}: passing an Intel GPU to a UEFI guest needs an option ROM with "
            "IgdAssignmentDxe (QEMU docs/igd-assign.txt). Build it from "
            "https://github.com/tomitamoeko/VfioIgdPkg (./build.sh igd.rom) and "
            "give its path with --reconfigure"
        )
    stops = _stops_desktop(ctx, kind)
    if stops and _in_desktop():
        return "this terminal is part of the desktop that gets stopped", (
            f"{name}: handing over the NVIDIA card stops the desktop this terminal "
            "runs in. Run this over SSH, in tmux, or on a text console "
            "(Ctrl+Alt+F3); or connect a display to an Intel output and log in again"
        )
    held = [] if stops else _holders(gpu)
    if held:
        return f"held by {', '.join(held)}", (
            f"{name}: the {kind} GPU is held by {', '.join(held)}. The desktop lets "
            "go of it at the next login (the udev rule of this stage tells GNOME "
            "to): log out and in, or reboot, then run this again. Stop anything "
            "else in the list yourself"
        )
    return None


def _desktop_warning(ctx, kind):
    if not _stops_desktop(ctx, kind):
        return []
    return [
        "WARNING: no display is connected to an Intel output, so the display",
        "manager is stopped while the card is away: THE LOCAL SCREEN GOES BLANK",
        "(with a guest: it shows Windows). It is started again afterwards.",
    ]


# ---------------------------------------------------------------- steps


def preflight(ctx, step, present):  # noqa: C901, PLR0912
    """A VM's answers make sense on this machine. Wrong answers stop the run."""
    if not present:
        return Probe(True, "nothing to check")
    name, vcpus = step["vm"], str(step["vcpus"])
    if not re.fullmatch(VM_NAME, name):
        raise Fail(f"VM name {name!r}: letters, digits and -, at most 15 characters")
    if not re.fullmatch(USER_NAME, step["username"]):
        raise Fail(f"Windows user name {step['username']!r}: letters, digits, _ . -")
    cpus = len(ctx.values["performance_cpus"])
    if not vcpus.isdigit() or int(vcpus) % 2 or not 1 < int(vcpus) <= cpus:
        raise Fail(f"{name}: vCPUs must be an even number from 2 to {cpus}")
    for key in ("memory_gib", "disk_gib"):
        if not str(step[key]).isdigit():
            raise Fail(f"{name}: {key} must be a whole number")
    if int(step["memory_gib"]) >= ctx.values["memory_gib"]:
        raise Fail(f"{name}: {step['memory_gib']} GiB is all the memory there is")
    try:
        ipaddress.ip_address(step["guest_ip"])
    except ValueError as e:
        raise Fail(f"{name}: guest address: {e}") from None
    others = [vm for vm in ctx.values["vm"] if vm["name"] != name]
    if step["guest_ip"] in [vm["guest_ip"] for vm in others]:
        raise Fail(f"{name}: another VM has the address {step['guest_ip']}")
    ports = [str(vm["rdp_port"]) for vm in others if vm["rdp"]]
    if step["rdp"] and str(step["rdp_port"]) in ports:
        raise Fail(f"{name}: another VM forwards RDP port {step['rdp_port']}")
    if step["rdp"] and not ctx.values["tailscale_ips"]:
        raise Fail(f"{name}: the RDP forward needs tailscale to be up")
    if step["gpu"] != "none" and not ctx.values["iommu"]:
        raise Fail(f"{name}: passing a GPU on needs the IOMMU (VT-d) switched on")
    if step["gpu"] not in gpu_choices(ctx.values):
        raise Fail(f"{name}: there is no {step['gpu']} GPU that can be passed on")
    if step["installed"]:
        return Probe(True, "Windows is installed")
    return _install_blocker(step) or Probe(True, "ready to install")


def _install_blocker(step):
    """What keeps Windows from being installed: a BLOCKED plan, or None."""
    name, iso, disk = step["vm"], Path(step["iso"]), Path(step["disk"])
    if not iso.is_file():
        return _blocked(f"no ISO at {iso}", f"{name}: there is no Windows ISO at {iso}")
    closed = [p for p in iso.parents if not p.stat().st_mode & 0o001]
    if closed:
        return _blocked(
            f"QEMU cannot reach {iso}",
            f"{name}: the ISO is under {closed[0]}, which libvirt's QEMU user "
            f"cannot enter; move it, for example to {disk.parent}",
        )
    folder = next(p for p in (disk, *disk.parents) if p.exists())
    free = shutil.disk_usage(folder).free // GIB
    if not disk.exists() and free < int(step["disk_gib"]):
        return _blocked(
            f"{free} GiB free in {folder}",
            f"{name}: a {step['disk_gib']} GiB disk does not fit in {folder} "
            f"({free} GiB free)",
        )
    return None


def windows_install(ctx, step, present):
    """Windows is installed on the VM's disk, unattended, from the ISO.

    Starts the guest, presses a key for the ISO's "Press any key to boot"
    prompt, and waits for the guest agent, which the last step of the answer
    file installs. Then the marker next to the disk is written.
    """
    name, marker = step["vm"], Path(f"{step['disk']}.installed")
    if not present:
        if not (step["purge"] and marker.exists()):
            return Probe(True, "nothing to undo")
        return Probe(False, "marked"), [Action(f"remove {marker}", ["rm", str(marker)])]
    if marker.exists():
        return Probe(True, "installed")
    minutes = int(step["timeout_min"])

    def install():
        if _state(ctx, name) == "shut off":
            ctx.sh.run([*VIRSH, "start", name])
            for _ in range(10):  # the prompt is there for a few seconds
                _sleep(1)
                ctx.sh.run([*VIRSH, "send-key", name, "KEY_ENTER"], check=False)
        watch = f"Windows Setup runs in {name} (watch it: virt-manager)"
        say(Note(f"{watch}; up to {minutes} minutes", 2))

        def answered():
            if _state(ctx, name) != "running":
                raise Fail(f"{name} stopped while Windows was being installed")
            return _agent(ctx, name, "guest-ping") is not None

        took = _wait(ctx, f"Windows Setup in {name}", answered, minutes * 60)
        say(Note(f"the guest agent answers after {took // 60} minutes", 2))
        ctx.sh.run(["touch", str(marker)])

    detail = [
        f"virsh start {name}; Enter is sent for 'Press any key to boot from CD'",
        f"wait up to {minutes} minutes for the guest agent (the last thing the",
        "answer file installs), then mark the disk as installed",
    ]
    desc = f"install Windows in {name}, unattended"
    return Probe(False, "not installed"), [Action(desc, fn=install, detail=detail)]


def udev_applied(ctx, step, present):  # noqa: ARG001
    """udev has read a rule file that changes in this run."""
    if f"file:{step['rule']}" not in ctx.changing:
        return Probe(True, "rules are loaded")
    reload = ["udevadm", "control", "--reload-rules"]
    trigger = ["udevadm", "trigger", "--subsystem-match=drm", "--action=change"]
    return Probe(False, "rules change in this run"), [
        Action("reload the udev rules", reload),
        Action("apply them to the display devices", trigger),
    ]


def _rehearsed(gpu):
    return STATE / f"rehearsed-{gpu['address']}-{gpu['device']}"


def gpu_rehearsal(ctx, step, present):
    """The host can let go of the VM's GPU and take it back, tried without a VM.

    `winvm rehearse` does what a guest start and stop do to the card: the
    hook's preparation, a detach to vfio-pci, the re-attach, the hook's
    release; then the host driver must be bound again (and nvidia-smi work).
    """
    kind, name = step["gpu"], step["vm"]
    gpu = _candidates(ctx.values).get(kind)
    if not present:
        mark = _rehearsed(gpu) if gpu else None
        if kind in step["still_used"] or mark is None or not mark.exists():
            return Probe(True, "nothing to undo")
        forget = Action(f"forget the rehearsal of {kind}", ["rm", "-f", str(mark)])
        return Probe(False, "rehearsed"), [forget]
    if gpu and _rehearsed(gpu).exists():
        return Probe(True, f"passed for {gpu['address']}")
    if not ctx.once(f"rehearsal:{kind}"):
        return Probe(True, "as above")
    blocker = _gpu_blocker(ctx, step)
    if blocker:
        return _blocked(*blocker)
    argv = [WINVM, "rehearse", *gpu["functions"]]
    argv += ["--unload", step["unload"]] if kind == "nvidia" else []
    if kind == "nvidia" and step["reset_method"]:
        argv += ["--reset-method", step["reset_method"]]

    def rehearse():
        try:
            ctx.sh.run(argv)
            ctx.sh.run(["touch", str(_rehearsed(gpu))])
        except Fail as e:
            _strip_gpu(ctx, name)
            raise Fail(f"{e}\n{_without_gpu(name)}") from None

    detail = [*_desktop_warning(ctx, kind), "sudo " + " ".join(argv)]
    detail += ["On failure everything is put back and the VM keeps working without"]
    detail += ["the GPU; the log has the details (journalctl -t winvm)."]
    desc = f"rehearse handing the {kind} GPU ({gpu['address']}) over and back"
    return Probe(False, "not rehearsed"), [Action(desc, fn=rehearse, detail=detail)]


def _display_adapters(ctx, name):
    """The guest's display adapters: [(PNP device id, error code, name)]."""
    script = (
        "Get-CimInstance Win32_PnPEntity -Filter \"PNPClass='Display'\" | "
        "ForEach-Object { $_.PNPDeviceID + '|' + $_.ConfigManagerErrorCode + '|' "
        "+ $_.Name }"
    )
    out = _guest_run(ctx, name, "powershell.exe", "-NoProfile", "-Command", script)
    rows = [line.strip().split("|") for line in (out or "").splitlines()]
    return [tuple(row) for row in rows if len(row) == 3]  # noqa: PLR2004


def _guest_gpu(ctx, name, kind):
    """A line about the passed-on GPU as Windows sees it; Fail if it does not."""
    adapters = _display_adapters(ctx, name)
    mine = [a for a in adapters if GUEST_VENDOR[kind] in a[0].upper()]
    ctx.log.event("verify", check="guest_gpu", vm=name, adapters=adapters)
    if not mine:
        seen = ", ".join(a[2] for a in adapters) or "no display adapter"
        raise Fail(f"{name}: Windows does not see the {kind} GPU (it has: {seen})")
    device, code, label = mine[0]
    if code != "0":
        raise Fail(f"{name}: {label} ({device}) reports error code {code}")
    return f"{name}: Windows reports {label} working"


def gpu_guest_check(ctx, step, present):
    """The VM was started once with its GPU and Windows reported the card.

    A failure takes the GPU out of the definition again and starts the VM
    without it.
    """
    kind, name = step["gpu"], step["vm"]
    marker = Path(step["state"]) / f"gpu-{kind}-{step['address']}.ok"
    if not present:
        marks = sorted(Path(step["state"]).glob("gpu-*.ok"))
        forget = [Action(f"remove {m}", ["rm", "-f", str(m)]) for m in marks]
        return Probe(not marks, "tried" if marks else "nothing to undo"), forget
    if marker.exists():
        return Probe(True, "Windows has reported the card")
    blocker = _gpu_blocker(ctx, step)
    if blocker:
        return _blocked(*blocker)

    def check():
        try:
            _power_off(ctx, name)
            ctx.sh.run([*VIRSH, "start", name])
            _wait(
                ctx,
                f"{name} booting with the {kind} GPU",
                lambda: _agent(ctx, name, "guest-ping") is not None,
                AGENT_S,
            )
            say(Check("ok", _guest_gpu(ctx, name, kind)))
            ctx.sh.run(["touch", str(marker)])
        except Fail as e:
            ctx.sh.run([*VIRSH, "destroy", name], check=False)
            _strip_gpu(ctx, name)
            ctx.sh.run([*VIRSH, "start", name], check=False)
            raise Fail(f"{e}\n{_without_gpu(name)}") from None

    detail = [*_desktop_warning(ctx, kind)]
    detail += [f"shut {name} down, start it with the GPU, ask Windows for the card"]
    detail += ["On failure the GPU is taken out of the VM again and the VM is"]
    detail += ["started without it: it stays usable over RDP."]
    desc = f"start {name} with the {kind} GPU and ask Windows about it"
    return Probe(False, "not tried yet"), [Action(desc, fn=check, detail=detail)]


def lg_build(ctx, step, present):
    """The Looking Glass client, built from the pinned commit into /opt."""
    prefix, link = Path(step["prefix"]), Path(step["link"])
    binary = prefix / "bin" / "looking-glass-client"
    linked = link.is_symlink() and link.readlink() == prefix
    if not present:
        acts = [Action(f"remove {prefix}", ["rm", "-rf", str(prefix)])]
        acts += [Action(f"remove {link}", ["rm", "-f", str(link)])]
        gone = not prefix.exists() and not link.is_symlink()
        return Probe(gone, "absent" if gone else "installed"), [] if gone else acts
    if binary.exists() and linked:
        return Probe(True, f"{step['version']} in {prefix}")
    src = ctx.cache_dir / f"looking-glass-{step['version']}"
    build = src / "client" / "build"

    def pinned():
        head = ctx.sh.out(["git", "-C", str(src), "rev-parse", "HEAD"])
        if head != step["commit"]:
            raise Fail(f"{src} is at {head}, not the pinned {step['commit']}")

    clone = ["git", "clone", "--recursive", "--branch", step["version"]]
    clone += [step["repository"], str(src)]
    cmake = ["cmake", "-S", str(src / "client"), "-B", str(build)]
    cmake += [f"-DCMAKE_INSTALL_PREFIX={prefix}", "-DENABLE_LIBDECOR=ON"]
    acts = [] if src.exists() else [Action("fetch the source", clone, sudo=False)]
    acts += [
        Action(f"check that it is commit {step['commit'][:12]}", fn=pinned),
        Action("configure", cmake, sudo=False),
        Action("build", ["cmake", "--build", str(build), "--parallel"], sudo=False),
        Action(f"install into {prefix}", ["cmake", "--install", str(build)]),
        Action(f"link {link}", ["ln", "-sfn", str(prefix), str(link)]),
    ]
    return Probe(False, "not built" if not binary.exists() else "not linked"), acts


def shm_file(ctx, step, present):  # noqa: ARG001
    """The shared memory file exists now, not only after the next boot."""
    path = Path(step["path"])
    if not present or path.exists():
        return Probe(True, "present" if path.exists() else "absent")
    create = ["systemd-tmpfiles", "--create", step["conf"]]
    return Probe(False, "missing"), [Action(f"create {path}", create)]


def lg_guest_tools(ctx, step, present):  # noqa: ARG001
    """The Looking Glass installer for Windows, where the tools ISO is built from."""
    src, dest = Path(step["installer"] or "/nonexistent"), Path(step["dest"])
    if not present:
        acts = [Action(f"remove {dest}", ["rm", "-f", str(dest)])]
        return Probe(not dest.exists(), "present" if dest.exists() else "absent"), (
            acts if dest.exists() else []
        )
    if not src.is_file():
        return _blocked(
            f"no installer at {src}",
            f"download looking-glass-idd-setup.exe ({step['version']}) from "
            f"https://looking-glass.io/downloads to {src}",
        )
    same = dest.exists() and dest.stat().st_size == src.stat().st_size
    copy = ["install", "-D", "-m", "0644", str(src), str(dest)]
    return Probe(same, "copied" if same else "missing"), (
        [] if same else [Action(f"copy {src.name} to {dest.parent}", copy)]
    )


# ---------------------------------------------------------------- health checks


def guest_up(ctx, entry):
    """A running VM answers through its guest agent."""
    name = entry["vm"]
    state = _state(ctx, name)
    if state != "running":
        return f"{name} is {state or 'not defined'}: not checked (winvm start {name})"
    r = _manage(ctx, "qemu-agent-command", name, '{"execute": "guest-ping"}')
    if r is None:
        return f"{name} is running; its guest agent needs sudo or the libvirt group"
    if r.returncode != 0:
        raise Fail(f"{name} is running but its guest agent does not answer")
    info = _agent(ctx, name, "guest-get-osinfo") or {}
    return f"{name} is running: {info.get('pretty-name', 'guest agent answers')}"


def rdp_reachable(ctx, entry):
    """An RDP server answers through the forward, on every tailnet address."""
    name, port = entry["vm"], int(entry["port"])
    if _state(ctx, name) != "running":
        return f"{name} is not running: RDP not checked"
    for address in entry["addresses"]:
        try:
            with socket.create_connection((address, port), timeout=10) as conn:
                conn.sendall(RDP_HELLO)
                reply = conn.recv(16)
        except OSError as e:
            raise Fail(f"{name}: RDP on {address} port {port}: {e}") from e
        if not reply or reply[0] != TPKT:
            raise Fail(f"{name}: {address} port {port} is open, but not RDP")
    where = ", ".join(entry["addresses"])
    return f"{name}: an RDP server answers on {where} port {port}"


def connect_help(ctx, entry):
    """How to get in from another machine (the line holds the password)."""
    text = read_file(ctx, entry["answers"], secret=True)
    found = re.search(r"<Password>\s*<Value>([^<]+)</Value>", text or "")
    password = found[1] if found else f"in {entry['answers']} (sudo)"
    host = ctx.values["magicdns"] or entry["addresses"][0]
    return (
        f"{entry['vm']} over RDP: {host}:{entry['port']}  user {entry['username']}  "
        f"password {password}"
    )


def gpu_host(ctx, entry):
    """The GPU is where it should be: in the running guest, or back on the host."""
    name, kind = entry["vm"], entry["gpu"]
    gpu = _candidates(ctx.values).get(kind)
    if gpu is None:
        raise Fail(f"{name}: there is no {kind} GPU that can be passed on")
    drivers = {f: _driver(f) or "no driver" for f in gpu["functions"]}
    held = VFIO in drivers.values()
    guests = [vm["name"] for vm in ctx.values["vm"] if vm["gpu"] == kind]
    running = [g for g in guests if _state(ctx, g) == "running"]
    if held and not running:
        raise Fail(f"{kind} GPU is on {VFIO} although no VM runs: {drivers}")
    if held:
        return f"{kind} GPU is in {running[0]} ({gpu['address']} on {VFIO})"
    if "no driver" in drivers.values():
        raise Fail(f"{kind} GPU has no driver on the host: {drivers}")
    if kind == "nvidia" and ctx.sh.probe(["nvidia-smi"]).returncode != 0:
        raise Fail("the NVIDIA card is on the host, but nvidia-smi fails")
    return f"{kind} GPU is on the host ({', '.join(sorted(set(drivers.values())))})"


def gpu_guest(ctx, entry):
    """A running VM that has its GPU reports the card working."""
    name, kind = entry["vm"], entry["gpu"]
    if _state(ctx, name) != "running":
        return f"{name} is not running: its GPU not checked"
    if _manage(ctx, "domid", name) is None:
        return f"{name}: asking Windows needs sudo or the libvirt group"
    return _guest_gpu(ctx, name, kind)


def lg_help(ctx, entry):  # noqa: ARG001
    return (
        f"{entry['vm']}: in Windows, run looking-glass-idd-setup.exe from the "
        f"'TOOLS' CD once (same version as the client, {entry['version']}); then "
        f"`winvm start {entry['vm']}` opens the screen"
    )
