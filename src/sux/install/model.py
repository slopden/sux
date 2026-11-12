import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sux.install.log import Fail, say
from sux.install.render import Check, Note

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
            say(Note(self.desc, 2))
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
        self._changing = set()
        self.asked = False  # whether `changing` was read since this was cleared
        self._done = set()

    @property
    def changing(self):
        """Keys of resources that change in this run."""
        self.asked = True
        return self._changing

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
    follows: bool = False  # it depends on what else changes in this run

    @property
    def settled(self):
        """Whether a later pass, with sudo, would find the same."""
        p = self.probe
        return p.ok is not None and UNKNOWN not in p.have and not self.follows


def probe_and_plan(resources, ctx, before=None):
    """Probe and plan every resource; with `before`, only what is not settled.

    `before` is the result of an earlier pass over the same resources: what
    was settled there is kept as it is, without probing it again.
    """
    out = []
    for r, old in zip(resources, before or [None] * len(resources), strict=True):
        if old and old.settled:
            if old.actions:
                ctx.changing.add(r.key)
            out.append(old)
            continue
        ctx.asked = False
        p = r.probe(ctx)
        # unknown (needs sudo) is left alone until it has been probed
        acts = r.plan(ctx, p) if p.ok is False else []
        follows = ctx.asked
        if acts:
            ctx.changing.add(r.key)
        out.append(Planned(r, p, acts, follows))
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
        now = f"{pl.probe.have}  {pl.probe.reason}".strip()
        say(Check("FAILED", f"not converged: {pl.res.label()}: {now}"))
    if bad:
        raise Fail(f"not converged: {', '.join(pl.res.key for pl in bad)}")
    text = f"re-probed: all {len(planned)} items match the desired state"
    say(Check("ok", text))
