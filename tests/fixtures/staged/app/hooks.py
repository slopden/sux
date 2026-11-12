"""Hooks shared by the staged test recipes."""

from pathlib import Path


def marker(ctx, step, present):
    path = Path(step["path"])
    if path.exists() == present:
        return Probe(True, "present" if present else "absent")
    argv = ["touch", str(path)] if present else ["rm", "-f", str(path)]
    return Probe(False, "missing" if present else "present"), [
        Action(f"{'create' if present else 'remove'} {path.name}", argv, sudo=False)
    ]


def healthy(ctx, entry):
    if Path(entry["path"] + ".broken").exists():
        raise Fail(f"{Path(entry['path']).name} is broken")
    return f"{Path(entry['path']).name} is healthy"


def twice(values):
    return f"{values['word']}{values['word']}"
