import argparse
import json
import os
import socket
import sys
import time
import tomllib
import traceback
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
lazy from collections.abc import Callable

from sux.install.facts import Facts
from sux.install.log import Fail, RunLog, say
from sux.install.model import Ctx, apply_plan, check_converged, probe_and_plan
from sux.install.prompt import ask_terminal, choose_from, on_terminal, picked
from sux.install.recipe import (
    Question,
    build_resources,
    build_status,
    build_verify,
    builtin_recipes,
    check_requires,
    dependencies,
    dependents,
    load_recipe,
    managed_units,
    resolve,
)
from sux.install.render import (
    Answered,
    Ask,
    Check,
    Heading,
    Listing,
    Menu,
    Note,
    Options,
    Outcome,
    Plan,
    Row,
    Situation,
    Step,
    Table,
)
from sux.install.shell import Shell
from sux.install.verify import run_verify

JOURNAL_LINES = 80  # per unit, in a status snapshot
YES = ("y", "yes", "true")
NO = ("n", "no", "false")

INSTALL_DOC = """\
With no recipe: list the recipes and their state.

With a recipe: ask its questions (first run only), check this machine without
sudo and print what would change, then offer apply / reprobe with sudo / exit.
Nothing is changed before you choose, or when stdin is not a terminal.
"""


# ---------------------------------------------------------------- answers


def answers_path(name):
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "sux" / "install" / f"{name}.toml"


def load_answers(name):
    path = answers_path(name)
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise Fail(f"{path}: {e}") from e


def dump_toml(answers):
    """Write the answers as TOML (stdlib has no writer).

    Values are str/bool/number/list; a list of tables (the instances of a
    recipe) is written as `[[name]]` sections after everything else.
    """

    def one(value):
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, list):
            return "[" + ", ".join(one(v) for v in value) + "]"
        return json.dumps(str(value))

    def table(items):
        return "".join(f"{k} = {one(v)}\n" for k, v in items.items())

    def tables(value):
        return isinstance(value, list) and value and isinstance(value[0], dict)

    out = table({k: v for k, v in answers.items() if not tables(v)})
    for name, rows in answers.items():
        if tables(rows):
            out += "".join(f"\n[[{name}]]\n{table(row)}" for row in rows)
    return out


def save_answers(name, answers):
    path = answers_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_toml(answers))
    return path


# ---------------------------------------------------------------- questions


def parse_answer(q, raw):  # noqa: C901, PLR0911
    """Turn what was typed into the answer; ValueError says what is wrong."""

    def pick(token):
        if token.isdigit() and 0 < int(token) <= len(q.choices):
            return q.choices[int(token) - 1]
        if token in q.choices:
            return token
        raise ValueError(f"{token!r} is not one of the choices")

    if not raw:
        if q.default is None:
            raise ValueError("an answer is needed")
        return q.default
    if q.kind == "bool":
        if raw.lower() not in YES + NO:
            raise ValueError("answer y or n")
        return raw.lower() in YES
    if q.kind == "path":
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ValueError("give an absolute path")
        return str(path)
    if q.kind == "choice":
        return pick(raw)
    if q.kind == "multi":
        if raw.lower() == "none":
            return []
        picked = [pick(token.strip()) for token in raw.split(",") if token.strip()]
        return [c for c in q.choices if c in picked]
    return raw


def ask(q, input_fn=input):
    """Ask one question until the answer is valid; Enter takes the default."""
    view = Ask(q.text, q.kind, tuple(q.choices or ()), q.default)
    if on_terminal(input_fn):
        return ask_terminal(view, lambda raw: parse_answer(q, raw))
    if view.listed:
        say(view)
    while True:
        try:
            return parse_answer(q, input_fn(view.prompt).strip())
        except ValueError as e:
            say(Note(str(e), 2))


# ---------------------------------------------------------------- output


def state_row(pl):
    status = "ok" if pl.probe.ok else ("change" if pl.actions else "unknown")
    return Row(status, pl.res.label(), pl.probe.have, pl.probe.reason)


def log_plan(planned, sh):
    """Record every command of the plan, with its whole diff."""
    actions = [(pl, a) for pl in planned for a in pl.actions]
    for n, (pl, a) in enumerate(actions, 1):
        sh.log.event(
            "plan_action",
            n=n,
            resource=pl.res.key,
            desc=a.desc,
            cmd=a.shown(sh),
            diff="\n".join(a.detail),
        )


def print_plan(planned, sh):
    """Print the commands apply would run, with diffs; return how many."""
    actions = [a for pl in planned for a in pl.actions]
    steps = (Step(n, a.shown(sh), tuple(a.detail)) for n, a in enumerate(actions, 1))
    plan = Plan(tuple(steps), sum(pl.probe.ok is None for pl in planned))
    if str(plan):
        say(plan)
    log_plan(planned, sh)
    return len(actions)


OPTION_HELP = {
    "apply": "run the plan above (asks for your sudo password)",
    "reprobe": "check the {unknown} unknown item(s) with sudo; changes nothing",
    "exit": "leave without changing anything",
    "add": "add a new one",
    "edit": "change the answers of one",
    "remove": "remove one, and what it installed",
    "done": "finish and show the plan",
}


def pick(options, input_fn, unknown=0, start=None):
    """List the options with what each does; returns the one chosen.

    On a terminal it is a list to move in, with `start` (default: the last
    option) under the cursor. Otherwise it prompts until one is typed; any
    unambiguous prefix of an option is accepted.
    """
    help_of = {o: OPTION_HELP[o].format(unknown=unknown) for o in options}
    pairs = tuple(help_of.items())
    if on_terminal(input_fn):
        hint = "Up/Down and Enter, or the first letter"
        menu = Menu("", pairs, options.index(start or options[-1]), hint=hint)
        choice = picked(choose_from(menu, hot=True))
        say(Answered(" / ".join(options), choice))
        return choice
    say(Options(pairs))
    prompt = " / ".join(options) + ": "
    while True:
        raw = input_fn(prompt).strip().lower()
        matches = [option for option in options if raw and option.startswith(raw)]
        if len(matches) == 1:
            return matches[0]


def choose(can_apply, unknown, input_fn=input):
    """The one prompt: returns "apply", "reprobe" or "exit".

    `unknown` is how many items only sudo can check; reprobe is offered for
    them. What is under the cursor at first never changes anything: exit, or
    reprobe while there is nothing to apply. Ctrl-C and Ctrl-D are exit.
    """
    options = ["apply"] if can_apply else []
    options += ["reprobe"] if unknown else []
    start = "reprobe" if unknown and not can_apply else "exit"
    try:
        return pick([*options, "exit"], input_fn, int(unknown), start)
    except EOFError, KeyboardInterrupt:
        return "exit"


def edit_instances(spec, rows, one, input_fn):  # noqa: C901
    """The add / edit / remove loop over a recipe's instances.

    `rows` are the saved answers, one table per instance; `one(row, asking)`
    asks an instance's questions. A removed instance stays in the returned
    rows, marked `removed`, until a run has taken away what it owned.
    """
    name, key = spec["name"], spec["key"]
    rows = [dict(row) for row in rows]

    def live():
        return [row for row in rows if not row.get("removed")]

    def place(got, old=None):
        """Put freshly asked answers in the list, in place of `old`."""
        same = [r for r in rows if r.get(key) == got[key] and r is not old]
        if any(not r.get("removed") for r in same):
            text = f"there is already a {name} with {key} {got[key]}; nothing changed"
            say(Note(text, 2))
            return
        for row in same:  # taking the place of one that was being removed
            rows.remove(row)
        if old is None:
            rows.append(got)
        elif old[key] == got[key]:
            rows[rows.index(old)] = got
        else:  # under a new key it is a new instance; the old one goes
            old["removed"] = True
            rows.append(got)

    def which():
        keys = [row[key] for row in live()]
        if len(keys) > 1:
            choice = ask(Question(key, f"Which {name}", "choice", keys, None), input_fn)
            return live()[keys.index(choice)]
        return live()[0]

    for row in live():  # a question this recipe did not have before
        if one(row)[2]:
            place(one(row, asking=True)[1], row)
    if not live():
        place(one({}, asking=True)[1])
    while True:
        say(Heading(f"{name}s:"))
        rest = [{k: v for k, v in row.items() if k != key} for row in live()]
        rest = [dump_toml(row).strip().replace(chr(10), ", ") for row in rest]
        keys = [row[key] for row in live()]
        if keys:
            say(Options(tuple(zip(keys, rest, strict=True)), gap=False))
        acts = ["add", "edit", "remove", "done"] if live() else ["add", "done"]
        choice = pick(acts, input_fn)
        if choice == "done":
            return rows
        if choice == "add":
            place(one({}, asking=True)[1])
        elif choice == "edit":
            row = which()
            place(one(row, asking=True)[1], row)
        else:
            which()["removed"] = True


def forget_removed(recipe, answers):
    """Drop instances marked removed, once what they owned is gone."""
    name = recipe.instances.get("name")
    rows = answers.get(name, [])
    kept = [row for row in rows if not row.get("removed")]
    if len(kept) != len(rows):
        save_answers(recipe.name, answers | {name: kept})


# ---------------------------------------------------------------- flow


def configure(recipe, facts, sh, opts):
    """Step 0: the variables, asking what has no saved answer.

    Returns (values, answers), or None when there is nobody to ask or the
    questions were left with Ctrl-C.
    """
    saved = load_answers(recipe.name)
    values, answers, unanswered = resolve(recipe, facts, saved)
    if opts["reconfigure"]:
        _, _, unanswered = resolve(recipe, facts, {})
    if unanswered and not opts["tty"]:
        say(Note(f"{recipe.name} has questions that are not answered yet:"))
        say(Options(tuple((q.name, q.text) for q in unanswered), gap=False))
        say(Note(f"Run `sux install {recipe.name}` in a terminal to answer them."))
        sh.log.event("unanswered", questions=[q.name for q in unanswered])
        return None
    if unanswered:
        input_fn = opts["input_fn"]
        try:
            values, answers, _ = resolve(
                recipe,
                facts,
                saved,
                ask=lambda q: ask(q, input_fn),
                reconfigure=opts["reconfigure"],
                menu=lambda rows, one: edit_instances(
                    recipe.instances, rows, one, input_fn
                ),
            )
        except KeyboardInterrupt:  # Ctrl-C at a question: nothing was saved
            say(Note("Nothing was changed.", gap=True))
            return None
        say(Note(f"answers saved to {save_answers(recipe.name, answers)}"))
    sh.log.event("answers", answers=answers)
    return values, answers


def collect_status(sh, units, extra, reason):
    """Write one `status` event: what an agent needs to diagnose this run.

    For every unit the recipe manages: `systemctl status` and the journal
    tail; then the listening sockets and the recipe's `[[status]]` commands.
    Read-only. sudo is used only if it is already in use, and never prompts;
    what would need it otherwise is listed as skipped.
    """
    root = sh.sudo_probes
    full = [u if "." in u else f"{u}.service" for u in units]  # smbd = smbd.service
    units = list(dict.fromkeys(full))
    tail = ["-n", str(JOURNAL_LINES), "--no-pager", "-o", "short-iso"]
    status = ["systemctl", "status", "--no-pager", "-l", *units]
    wanted = [(status, False)] if units else []
    wanted += [(["journalctl", "-u", unit, *tail], root) for unit in units]
    wanted.append((["ss", "-ltnp"], root))
    wanted += [(list(e["argv"]), e.get("sudo", False)) for e in extra]
    commands, skipped = [], []
    for cmd, sudo in wanted:
        if sudo and not root:
            skipped.append(cmd)
            continue
        argv = ["sudo", "-n", *cmd] if sudo and not sh.is_root else cmd
        try:
            r = sh.spawn(argv)
            rc, output = r.returncode, (r.stdout or "") + (r.stderr or "")
        except OSError as e:  # the tool is not installed
            rc, output = 127, str(e)
        commands.append({"argv": argv, "rc": rc, "output": sh.log.clip(output)})
    sh.log.event(
        "status", reason=reason, units=units, commands=commands, skipped=skipped
    )


@dataclass
class Unit:
    """What is applied and verified as a whole: a stage, or a whole recipe."""

    resources: list
    new_ctx: Callable
    verify: list = field(default_factory=list)
    title: str = ""  # heading of its part of the plan
    confirm: bool = False  # ask again before it is applied after another unit
    needed_by: str = ""  # a dependency: one line, and nothing more, when in place


@dataclass
class Prepared:
    """One recipe of a run, with its variables worked out."""

    recipe: object
    values: dict
    answers: dict
    units: list

    @property
    def resources(self):
        return [r for unit in self.units for r in unit.resources]


def prepare(recipe, sh, facts, stamp, opts, needed_by=""):  # noqa: PLR0913, PLR0917
    """A recipe's units: one per stage, or one for all of it. None: questions."""
    remove = opts["remove"]
    asking = opts if not needed_by else opts | {"reconfigure": False}
    configured = configure(recipe, facts, sh, asking)
    if configured is None:
        return None
    values, answers = configured
    if not remove:
        check_requires(recipe, facts)
    hooks = recipe.hooks()
    cache_dir = Path(facts.get("home")) / ".cache" / "sux"

    def new_ctx():
        extra = {"hooks": hooks, "values": values, "cache_dir": cache_dir}
        return Ctx(sh, recipe.name, stamp=stamp, **extra)

    stages = [] if remove else recipe.stages
    tag = f"{recipe.name} (needed by {needed_by})" if needed_by else ""
    units = []
    for n, stage in enumerate(stages, 1):
        name = stage["name"]
        title = f"Stage {n}/{len(stages)}  {name}: {stage.get('title', '')}"
        units.append(
            Unit(
                build_resources(recipe, values, stage=name),
                new_ctx,
                build_verify(recipe, values, name),
                f"{tag}  {title}" if tag else title,
                stage.get("confirm", False),
                needed_by,
            )
        )
    if not stages:
        entries = [] if remove else build_verify(recipe, values)
        resources = build_resources(recipe, values, remove)
        units.append(Unit(resources, new_ctx, entries, tag, needed_by=needed_by))
    return Prepared(recipe, values, answers, units)


def installed(recipe, sh):
    """Whether removing `recipe` would still change this machine."""
    values, _, unanswered = resolve(recipe, Facts(sh), load_answers(recipe.name))
    if unanswered:
        return False
    ctx = Ctx(sh, recipe.name, hooks=recipe.hooks(), values=values)
    try:
        planned = probe_and_plan(build_resources(recipe, values, remove=True), ctx)
    except Fail:
        return True  # it cannot even be checked: not something to pull the rug from
    return any(not pl.probe.ok for pl in planned)


def run_flow(recipe, sh, **opts):
    """Questions, check, prompt, apply, converge, verify.

    Returns (exit code, whether the machine is now as the recipe wants it).

    The recipes it needs come first, in the same plan and under the same
    prompt; `--remove` leaves them alone, and is refused for a recipe that an
    installed one needs.
    """
    remove, input_fn, tty = opts["remove"], opts["input_fn"], opts["tty"]
    facts = Facts(sh)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    if remove:
        users = [r.name for r in dependents(recipe) if installed(r, sh)]
        if users:
            first = f"sux install {users[0]} --remove"
            raise Fail(
                f"{recipe.name} is needed by {', '.join(users)}, which is "
                f"installed; remove that first ({first})"
            )
    first = [] if remove else dependencies(recipe)
    parts = [prepare(dep, sh, facts, stamp, opts, recipe.name) for dep in first]
    parts.append(prepare(recipe, sh, facts, stamp, opts))
    if None in parts:
        return 1, False
    main = parts[-1]

    def snapshot(reason):
        try:
            extra, resources = [], []
            for part in parts:
                extra += build_status(part.recipe, part.values)
                resources += part.resources
            units = managed_units(resources)
            collect_status(sh, units, extra, reason)
        except Exception as e:  # noqa: BLE001  never in the way of the real outcome
            sh.log.event("status", reason=reason, error=repr(e))

    done = False
    try:
        done = settle_units(
            [unit for part in parts for unit in part.units],
            sh,
            input_fn if tty else None,
            applied=lambda: snapshot("applied"),
        )
        if done and not remove:
            forget_removed(main.recipe, main.answers)
    except Exception:
        snapshot("failed")
        raise
    return 0, done


def settle(resources, sh, new_ctx, input_fn, applied=None):
    """`settle_units` for one group of resources without health checks."""
    return settle_units([Unit(resources, new_ctx)], sh, input_fn, applied)


def settle_units(units, sh, input_fn, applied=None):  # noqa: C901, PLR0912, PLR0915
    """Check, prompt, then apply and verify unit by unit, in order.

    One prompt covers the whole plan. Once sudo is in use, what could not be
    checked without it is probed again, and only that and what it does to the
    plan is printed. A unit is probed again right before it
    is applied; if its plan is no longer the one that was shown (or it asks
    for `confirm`), it is shown and asked about again. Its health checks run
    before the next unit starts, so the first failure stops the run with the
    earlier units in place; a later run finds those converged and resumes.

    Returns False when the user (or a stdin that is not a terminal, passed as
    `input_fn=None`) left the machine as it is with changes still pending.
    `applied()` is called each time a unit has been applied and has converged.
    """

    def commands(planned):
        return [a.shown(sh) for pl in planned for a in pl.actions]

    def show(unit, planned):
        if unit.title:
            say(Heading(unit.title))
        say(Table(tuple(state_row(pl) for pl in planned)))
        print_plan(planned, sh)

    def check(unit):
        planned = probe_and_plan(unit.resources, unit.new_ctx())
        pending = commands(planned) or any(pl.probe.ok is None for pl in planned)
        if unit.needed_by and not pending:
            say(Check("ok", f"{unit.title}: nothing to change"))
        else:
            show(unit, planned)
        return planned

    def recheck(plans):
        """Probe again, with sudo, what was unknown or follows from it.

        Prints what those turned out to be and how the plan changed; the log
        gets the whole plan again.
        """
        say(Heading("Checked with sudo"))
        after = []
        for unit, old in zip(units, plans, strict=True):
            planned = probe_and_plan(unit.resources, unit.new_ctx(), old)
            for was, now in zip(old, planned, strict=True):
                if was.probe.ok is None or commands([was]) != commands([now]):
                    say(state_row(now))
                    for action in now.actions:
                        if action.detail:
                            say(Step("", action.shown(sh), tuple(action.detail)))
            log_plan(planned, sh)
            after.append(planned)
        old = [c for planned in plans for c in commands(planned)]
        new = [c for planned in after for c in commands(planned)]
        delta = {"+": Counter(new) - Counter(old), "-": Counter(old) - Counter(new)}
        if not any(delta.values()):
            say(Note(f"Plan unchanged ({len(new)} commands)"))
        else:
            say(Note(f"Plan changed ({len(new)} commands); + added, - removed"))
            for sign, cmds in delta.items():
                for cmd in cmds.elements():
                    say(Step(sign, cmd))
        return after

    plans = [check(unit) for unit in units]
    while True:
        shown = [commands(planned) for planned in plans]
        unknown = sum(pl.probe.ok is None for planned in plans for pl in planned)
        if not any(shown) and not unknown:
            break
        if input_fn is None:
            say(Note("Nothing was changed: stdin is not a terminal.", gap=True))
            return False
        say(Situation(sum(map(len, shown)), unknown))
        choice = choose(any(shown), unknown, input_fn)
        sh.log.event("choice", choice=choice)
        if choice == "exit":
            say(Note("Nothing was changed."))
            return False
        sh.elevate()  # sudo only after the choice
        if choice == "reprobe" or unknown:
            plans = recheck(plans)
            if choice == "reprobe":
                continue
            if [commands(planned) for planned in plans] != shown:
                text = "The plan changed once sudo could check everything."
                say(Note(text, gap=True))
                continue
        break

    current = True  # nothing was applied since `plans` were made
    for unit, checked, approved in zip(units, plans, shown, strict=True):
        applies, planned = False, checked
        try:
            if not current:
                planned = probe_and_plan(unit.resources, unit.new_ctx())
                changed = commands(planned) != approved
                if commands(planned) and (changed or unit.confirm):
                    show(unit, planned)
                    if changed:
                        text = "This part of the plan changed after the ones before it."
                        say(Note(text))
                    if choose(True, False, input_fn) == "exit":
                        text = "Stopped here; what was applied so far stays in place."
                        say(Note(text))
                        return False
            applies = bool(commands(planned))
            if applies:
                say(Heading("Applying" + (f": {unit.title}" if unit.title else "")))
                current = False
                apply_plan(planned, sh)
                check_converged(unit.resources, unit.new_ctx())
                if applied:
                    applied()
            if unit.verify and (applies or not unit.needed_by):
                title = f": {unit.title}" if unit.title else ""
                say(Heading(f"Health checks{title}"))
                run_verify(unit.verify, unit.new_ctx())
        except Exception as e:
            if unit.title:
                sh.log.event("unit", title=unit.title, ok=False, error=str(e))
                say(Heading(f"Stopped at {unit.title}"))
                text = (
                    "What was applied before it stays in place; a re-run resumes here."
                )
                say(Note(text))
            raise
        if unit.title:
            sh.log.event("unit", title=unit.title, ok=True, applied=applies)
    return True


def install_recipe(  # noqa: PLR0913
    ref, *, remove=False, reconfigure=False, input_fn=input, tty=None, make_shell=Shell
):
    """Run one recipe with a log that always ends in an `end` event."""
    try:
        recipe = load_recipe(ref)
    except Fail as e:
        say(Outcome("FAILED", str(e)))
        return 1
    log = RunLog.for_recipe(recipe.name)
    sh = make_shell(log)
    log.event(
        "start",
        recipe=recipe.name,
        recipe_sha256=recipe.sha256,
        mode="remove" if remove else "install",
        argv=sys.argv,
        host=socket.gethostname(),
        euid=os.geteuid(),
        python=sys.version.split()[0],
    )
    t0, status, error, shown = time.monotonic(), "ok", None, Outcome("")
    tty = sys.stdin.isatty() if tty is None else tty
    try:
        code, done = run_flow(
            recipe,
            sh,
            remove=remove,
            reconfigure=reconfigure,
            input_fn=input_fn,
            tty=tty,
        )
        status = "ok" if code == 0 else "incomplete"
        shown = Outcome("ok" if done else "", "converged")
    except Fail as e:
        status, error = "failed", str(e)
        shown = Outcome("FAILED", str(e))
    except (KeyboardInterrupt, EOFError) as e:
        status, error = "aborted", repr(e)
        shown = Outcome("ABORTED")
    except Exception as e:  # noqa: BLE001
        status, error = "crashed", traceback.format_exc()
        shown = Outcome("CRASHED", repr(e))
    finally:
        duration = round(time.monotonic() - t0, 3)
        log.event("end", status=status, error=error, duration_s=duration)
        log.close()
        say(Outcome(shown.status, shown.detail, log.path))
    return 0 if status == "ok" else 1


def recipe_state(recipe, sh):
    """One-line state for the listing, from an unprivileged check."""
    facts = Facts(sh)
    values, _, unanswered = resolve(recipe, facts, load_answers(recipe.name))
    if unanswered:
        return "not set up"
    ctx = Ctx(sh, recipe.name, hooks=recipe.hooks(), values=values)
    planned = probe_and_plan(build_resources(recipe, values), ctx)
    changes = sum(bool(pl.actions) for pl in planned)
    unknown = sum(pl.probe.ok is None for pl in planned)
    parts = [f"{changes} to change"] if changes else []
    parts += [f"{unknown} unknown until sudo"] if unknown else []
    return ", ".join(parts) or "installed"


def list_recipes(sh):
    rows = []
    for name in builtin_recipes():
        description = ""
        try:
            recipe = load_recipe(name)
            description = recipe.description
            state = recipe_state(recipe, sh)
        except Fail as e:
            state = f"error: {e}"
        rows.append((name, state, description))
    say(Listing(tuple(rows)))
    return 0


def install_main(argv):
    parser = argparse.ArgumentParser(
        prog="sux install",
        description="Declarative installers: check, show the plan, then ask.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=INSTALL_DOC,
    )
    parser.add_argument(
        "recipe", nargs="?", help="recipe name, or a path to a recipe directory"
    )
    parser.add_argument(
        "--remove", action="store_true", help="plan removing what the recipe installs"
    )
    parser.add_argument(
        "--reconfigure",
        action="store_true",
        help="ask the questions again, with the saved answers as defaults",
    )
    args = parser.parse_args(argv)
    if args.recipe is None:
        return list_recipes(Shell())
    return install_recipe(args.recipe, remove=args.remove, reconfigure=args.reconfigure)
