"""Hooks for the demo recipe.

The engine provides Action, Probe, Fail, say and read_file as globals, so a
hooks.py imports only what it needs from the standard library.
"""

from pathlib import Path


def marker(ctx, step, present):
    """Step hook: return a Probe, or (Probe, [Action, ...])."""
    path = Path(step["path"])
    if path.exists() == present:
        return Probe(True, "present" if present else "absent")
    argv = ["touch", str(path)] if present else ["rm", "-f", str(path)]
    return Probe(False, "missing" if present else "present"), [
        Action(f"{'create' if present else 'remove'} {path}", argv, sudo=False)
    ]


def check_conf(ctx, entry):
    """Verify hook: raise Fail when unhealthy, else return a message."""
    conf = ctx.values["conf"]
    if "root = " not in (read_file(ctx, conf) or ""):
        raise Fail(f"{conf} has no root")
    return f"{conf} names its root"
