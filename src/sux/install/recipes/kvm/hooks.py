"""Hooks for kvm: the checks that are not a file, a package or a unit.

The engine provides Action, Probe, Fail, say and read_file as globals.
"""

from pathlib import Path

KVM = Path("/dev/kvm")


def preflight(ctx, step, present):  # noqa: ARG001
    """Hardware virtualization is there; say what the IOMMU state is."""
    cpu = ctx.values["virtualization"]
    if not cpu:
        raise Fail("this CPU has no hardware virtualization (no vmx or svm flag)")
    loaded = Path("/sys/module/kvm").is_dir()
    if loaded and not KVM.exists():
        raise Fail(f"{cpu} is switched off in the firmware setup: {KVM} is missing")
    iommu = "on" if ctx.values["iommu"] else "off (needed only to pass devices)"
    return Probe(True, f"{cpu}, IOMMU {iommu}")


def network_active(ctx, entry):
    """The network guests attach to is up; says which addresses it hands out."""
    network = entry["network"]
    virsh = ["virsh", "-r", "-c", "qemu:///system"]
    info = ctx.sh.out([*virsh, "net-info", network])
    if "Active:" not in info or "yes" not in info.split("Active:")[1].split("\n")[0]:
        raise Fail(f"libvirt network {network} is not active")
    bridge = info.split("Bridge:")[1].split()[0] if "Bridge:" in info else "?"
    return f"libvirt network {network} is active on {bridge}"
