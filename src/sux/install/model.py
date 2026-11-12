import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sux.install.log import Fail, say

BACKUP_ROOT = Path("/var/backups/sux")
UNKNOWN = "unknown until sudo"


@dataclass
class Action:
    """One step of the plan: a command (argv) or a Python-side step (fn)."""

    desc: str
    argv: list | None = None
    sudo: bool = True
    stdin: str | None = None
    secret: bool = False
    check: bool = True
    interactive: bool = False  # talks to the terminal; output is not captured
    fn: Callable | None = None
    detail: list = field(default_factory=list)  # e.g. a diff, shown in the plan

    def shown(self, sh):
        if self.argv is None:
            return self.desc
        s = shlex.join(sh.argv(self.argv, self.sudo))
        if self.stdin is not None:
            s += "  < secret" if self.secret else "  < content"
        return s

    def execute(self, sh):
        if self.fn is not None:
            say(f"  {self.desc}")
            sh.log.event("cmd", kind="python", desc=self.desc)
            self.fn()
            return
        sh.run(
            self.argv,
            sudo=self.sudo,
            stdin=self.stdin,
            secret_stdin=self.secret,
            check=self.check,
            interactive=self.interactive,
        )


@dataclass
class Probe:
    ok: bool | None  # True: as desired; False: needs change; None: needs sudo
    have: str
    reason: str = ""


class Ctx:
    """What resources share during one probe-and-plan pass."""

    def __init__(  # noqa: PLR0913
        self, sh, recipe="recipe", *, hooks=None, values=None, cache_dir=None, stamp=""
    ):
        self.sh = sh
        self.log = sh.log
        self.recipe = recipe
        self.hooks = hooks or {}
        self.values = values if values is not None else {}
        self.cache_dir = cache_dir or Path.home() / ".cache" / "sux"
        stamp = stamp or datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
        self.backup_dir = BACKUP_ROOT / recipe / stamp
        self.changing = set()  # keys of resources that change in this run
        self._done = set()

    def once(self, name):
        """Return True the first time `name` is seen in this pass."""
        if name in self._done:
            return False
        self._done.add(name)
        return True


class Resource:
    """One piece of desired state, built from a recipe step.

    `present=False` is the inverse target (step switched off, or --remove).
    Subclasses set `self.target`, and implement `probe` and `plan`.
    """

    kind = "?"

    def __init__(self, step, present=True):
        self.step = step
        self.present = present
        self.target = ""

    @property
    def key(self):
        return f"{self.kind}:{self.target}"

    def label(self):
        return f"{self.kind} {self.target}" + ("" if self.present else "  (absent)")

    def probe(self, ctx):
        raise NotImplementedError

    def plan(self, ctx, p):  # noqa: ARG002
        return []


KINDS = {}


def register(cls):
    """Make a Resource class available to recipes as `kind = "<cls.kind>"`."""
    KINDS[cls.kind] = cls
    return cls


@dataclass
class Planned:
    res: Resource
    probe: Probe
    actions: list


def probe_and_plan(resources, ctx):
    out = []
    for r in resources:
        p = r.probe(ctx)
        # unknown (needs sudo) is left alone until it has been probed
        acts = r.plan(ctx, p) if p.ok is False else []
        if acts:
            ctx.changing.add(r.key)
        out.append(Planned(r, p, acts))
        ctx.log.event(
            "resource",
            kind=r.kind,
            target=r.target,
            present=r.present,
            have=p.have,
            ok=p.ok,
            reason=p.reason,
            actions=[a.shown(ctx.sh) for a in acts],
        )
    return out


def apply_plan(planned, sh):
    for pl in planned:
        for a in pl.actions:
            a.execute(sh)


def check_converged(resources, ctx):
    planned = probe_and_plan(resources, ctx)
    bad = [pl for pl in planned if not pl.probe.ok]
    ctx.log.event("converged", ok=not bad, remaining=[pl.res.key for pl in bad])
    for pl in bad:
        say(f"  NOT CONVERGED  {pl.res.label()}: {pl.probe.have}  {pl.probe.reason}")
    if bad:
        raise Fail(f"not converged: {', '.join(pl.res.key for pl in bad)}")
    say(f"  ok  re-probed: all {len(planned)} items match the desired state")
