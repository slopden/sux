import grp
import json
import os
import platform
import pwd
import re
import socket
from pathlib import Path

from sux.utils import host_username

SYSFS = Path("/sys")
PROC = Path("/proc")
PCI_DISPLAY = "0x03"  # PCI class: display controller
GPU_VENDORS = {"0x10de": "nvidia", "0x8086": "intel", "0x1002": "amd"}


def fact_invoking_user(_sh):
    return os.environ.get("SUDO_USER") or host_username()


def fact_invoking_group(sh):
    return grp.getgrgid(pwd.getpwnam(fact_invoking_user(sh)).pw_gid).gr_name


def fact_home(sh):
    return pwd.getpwnam(fact_invoking_user(sh)).pw_dir


def fact_hostname(_sh):
    return socket.gethostname()


def fact_arch(sh):
    return sh.out(["dpkg", "--print-architecture"])


def fact_codename(_sh):
    try:
        return platform.freedesktop_os_release().get("VERSION_CODENAME", "")
    except OSError:
        return ""


def fact_systemd(sh):
    words = sh.out(["systemctl", "--version"]).split()
    return int(words[1]) if len(words) > 1 and words[1].isdigit() else 0


def fact_tailscale_ips(sh):
    r = sh.probe(["tailscale", "ip"])
    return r.stdout.split() if r.returncode == 0 else []


def fact_magicdns(sh):
    """MagicDNS name of this machine (not the bare hostname), or ""."""
    r = sh.probe(["tailscale", "status", "--self", "--peers=false", "--json"])
    try:
        return json.loads(r.stdout)["Self"]["DNSName"].rstrip(".")
    except KeyError, ValueError, TypeError:
        return ""


def fact_iommu(_sh):
    groups = SYSFS / "kernel" / "iommu_groups"
    return groups.is_dir() and any(groups.iterdir())


def fact_gpus(_sh):
    """Display controllers: PCI address, vendor, bound driver, IOMMU group."""

    def link_name(path):
        return path.readlink().name if path.is_symlink() else ""

    devices = SYSFS / "bus" / "pci" / "devices"
    gpus = []
    for dev in sorted(devices.iterdir()) if devices.is_dir() else []:
        if not (dev / "class").read_text().startswith(PCI_DISPLAY):
            continue
        vendor = (dev / "vendor").read_text().strip()
        gpus.append(
            {
                "address": dev.name,
                "vendor": GPU_VENDORS.get(vendor, vendor),
                "driver": link_name(dev / "driver"),
                "iommu_group": link_name(dev / "iommu_group"),
            }
        )
    return gpus


def _read(path):
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def fact_gpu_devices(sh):
    """`gpus`, each with what handing it to a guest depends on.

    Its device id, every device in its IOMMU group, the functions of the same
    card (its audio), its connectors with their state, and its DRM nodes.
    """
    devices = SYSFS / "bus" / "pci" / "devices"
    out = []
    for gpu in fact_gpus(sh):
        dev = devices / gpu["address"]
        slot = dev.name.rpartition(".")[0]
        group = dev / "iommu_group" / "devices"
        status = sorted(dev.glob("drm/card*/card*-*/status"))
        extra = {
            "device": _read(dev / "device"),
            "group_devices": sorted(p.name for p in group.glob("*")),
            "functions": sorted(p.name for p in devices.glob(f"{slot}.*")),
            "connectors": {s.parent.name.partition("-")[2]: _read(s) for s in status},
            "drm": sorted(p.name for p in dev.glob("drm/*") if not p.is_symlink()),
        }
        out.append(gpu | extra)
    return out


def cpu_list(text):
    """ "0-3,8" as [0, 1, 2, 3, 8]."""
    cpus = []
    for part in filter(None, text.strip().split(",")):
        first, _, last = part.partition("-")
        cpus += range(int(first), int(last or first) + 1)
    return cpus


def fact_performance_cpus(_sh):
    """CPUs of the performance cores (all of them where cores are alike)."""
    online = SYSFS / "devices" / "system" / "cpu" / "online"
    hybrid = SYSFS / "devices" / "cpu_core" / "cpus"
    return cpu_list(_read(hybrid) or _read(online))


def fact_memory_gib(_sh):
    for line in _read(PROC / "meminfo").splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) // (1 << 20)
    return 0


def fact_kvm(_sh):
    """Whether this user can start hardware-accelerated guests right now."""
    return os.access("/dev/kvm", os.R_OK | os.W_OK)


def fact_virtualization(_sh):
    """ "vmx" or "svm" when the CPU has it (the firmware may still disable it)."""
    flags = re.search(r"^flags\s*:(.*)$", _read(PROC / "cpuinfo"), re.MULTILINE)
    found = {"vmx", "svm"} & set(flags[1].split() if flags else [])
    return next(iter(sorted(found)), "")


FACTS = {
    "invoking_user": fact_invoking_user,
    "invoking_group": fact_invoking_group,
    "home": fact_home,
    "hostname": fact_hostname,
    "arch": fact_arch,
    "codename": fact_codename,
    "systemd": fact_systemd,
    "tailscale_ips": fact_tailscale_ips,
    "magicdns": fact_magicdns,
    "iommu": fact_iommu,
    "gpus": fact_gpus,
    "gpu_devices": fact_gpu_devices,
    "performance_cpus": fact_performance_cpus,
    "memory_gib": fact_memory_gib,
    "kvm": fact_kvm,
    "virtualization": fact_virtualization,
}


class Facts:
    """Host values, looked up on first use and then cached for the run."""

    def __init__(self, sh):
        self.sh = sh
        self._cache = {}

    def get(self, name):
        """Value of a fact; KeyError if there is no such fact."""
        if name not in self._cache:
            self._cache[name] = FACTS[name](self.sh)
        return self._cache[name]
