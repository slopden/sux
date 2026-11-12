import argparse
import json
import os
import socket
import sys
import time
import tomllib
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sux.install.facts import Facts
from sux.install.log import Fail, RunLog, say
from sux.install.model import Ctx, apply_plan, check_converged, probe_and_plan
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
from sux.install.shell import Shell
from sux.install.verify import run_verify

DIFF_LINES = 60  # of one diff shown in the plan; the log has all of it
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
    hint = "" if q.default is None else f" [{q.default}]"
    if q.kind == "bool" and q.default is not None:
        hint = " [Y/n]" if q.default else " [y/N]"
    if q.kind in ("choice", "multi"):
        say(f"{q.text}:")
        for n, choice in enumerate(q.choices, 1):
            ticked = "[x] " if choice in (q.default or []) else "[ ] "
            say(f"  {n}. {ticked if q.kind == 'multi' else ''}{choice}")
    if q.kind == "multi":
        hint = " (numbers or names, comma separated; 'none'; Enter keeps [x])"
    prompt = ("  choose" if q.kind in ("choice", "multi") else q.text) + hint + ": "
    while True:
        try:
            return parse_answer(q, input_fn(prompt).strip())
        except ValueError as e:
            say(f"  {e}")


# ---------------------------------------------------------------- output


def print_state(planned):
    say(f"\n  {'':<6}  {'desired state':<52}  now")
    for pl in planned:
        mark = "ok" if pl.probe.ok else ("CHANGE" if pl.actions else "?")
        have = pl.probe.have + (f"  ({pl.probe.reason})" if pl.probe.reason else "")
        say(f"  {mark:<6}  {pl.res.label():<52}  {have}")


def print_plan(planned, sh):
    """Print the commands apply would run, with diffs; return how many."""
    actions = [(pl, a) for pl in planned for a in pl.actions]
    unknown = sum(pl.probe.ok is None for pl in planned)
    if actions:
        say(f"\nPlan ({len(actions)} commands)")
    elif not unknown:
        say("\nPlan: nothing to change")
    for n, (pl, a) in enumerate(actions, 1):
        say(f"  {n:>2}  {a.shown(sh)}")
        for line in a.detail[:DIFF_LINES]:
            say(f"        {line}")
        if len(a.detail) > DIFF_LINES:
            say(f"        ... {len(a.detail) - DIFF_LINES} more lines (in the log)")
        sh.log.event(
            "plan_action",
            n=n,
            resource=pl.res.key,
            desc=a.desc,
            cmd=a.shown(sh),
            diff="\n".join(a.detail),
        )
    if unknown:
        say(f"  {unknown} item(s) cannot be checked without sudo")
    return len(actions)


def pick(options, input_fn):
    """Prompt until one option is typed (its first letter or first word)."""
    prompt = "\n" + " / ".join(f"[{o[0]}]{o[1:]}" for o in options) + ": "
    while True:
        raw = input_fn(prompt).strip().lower()
        for option in options:
            if raw and raw in (option[0], option.split()[0]):
                return option.split()[0]


def choose(can_apply, can_reprobe, input_fn=input):
    """The one prompt: returns "apply", "reprobe" or "exit"."""
    options = ["apply"] if can_apply else []
    options += ["reprobe with sudo"] if can_reprobe else []
    try:
        return pick([*options, "exit"], input_fn)
    except EOFError:
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
            say(f"  there is already a {name} with {key} {got[key]}; nothing changed")
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
        say(f"\n{name}s:")
        for row in live():
            rest = {k: v for k, v in row.items() if k != key}
            say(f"  {row[key]:<14}  {dump_toml(rest).strip().replace(chr(10), ', ')}")
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

    Returns (values, answers), or None when there is nobody to ask.
    """
    saved = load_answers(recipe.name)
    values, answers, unanswered = resolve(recipe, facts, saved)
    if opts["reconfigure"]:
        _, _, unanswered = resolve(recipe, facts, {})
    if unanswered and not opts["tty"]:
        say(f"{recipe.name} has questions that are not answered yet:")
        for q in unanswered:
            say(f"  {q.name:<14}  {q.text}")
        say(f"Run `sux install {recipe.name}` in a terminal to answer them.")
        sh.log.event("unanswered", questions=[q.name for q in unanswered])
        return None
    if unanswered:
        input_fn = opts["input_fn"]
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
        say(f"answers saved to {save_answers(recipe.name, answers)}")
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


def prepare(recipe, sh, facts, stamp, opts, needed_by=""):  # noqa: PLR0913
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
    """Questions, check, prompt, apply, converge, verify. Returns an exit code.

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
        return 1
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
    return 0


def settle(resources, sh, new_ctx, input_fn, applied=None):
    """`settle_units` for one group of resources without health checks."""
    return settle_units([Unit(resources, new_ctx)], sh, input_fn, applied)


def settle_units(units, sh, input_fn, applied=None):  # noqa: C901, PLR0912, PLR0915
    """Check, prompt, then apply and verify unit by unit, in order.

    One prompt covers the whole plan. A unit is probed again right before it
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
            say(f"\n{unit.title}")
        print_state(planned)
        print_plan(planned, sh)

    def check(unit):
        planned = probe_and_plan(unit.resources, unit.new_ctx())
        pending = commands(planned) or any(pl.probe.ok is None for pl in planned)
        if unit.needed_by and not pending:
            say(f"  ok  {unit.title}: nothing to change")
        else:
            show(unit, planned)
        return planned

    plans = [check(unit) for unit in units]
    while True:
        shown = [commands(planned) for planned in plans]
        unknown = any(pl.probe.ok is None for planned in plans for pl in planned)
        if not any(shown) and not unknown:
            break
        if input_fn is None:
            say("\nNothing was changed: stdin is not a terminal.")
            return False
        choice = choose(any(shown), unknown, input_fn)
        sh.log.event("choice", choice=choice)
        if choice == "exit":
            say("Nothing was changed.")
            return False
        sh.elevate()  # sudo only after the choice
        if choice == "reprobe" or unknown:
            plans = [check(unit) for unit in units]
            if choice == "reprobe":
                continue
            if [commands(planned) for planned in plans] != shown:
                say("\nThe plan changed once sudo could check everything.")
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
                        say("This part of the plan changed after the ones before it.")
                    if choose(True, False, input_fn) == "exit":
                        say("Stopped here; what was applied so far stays in place.")
                        return False
            applies = bool(commands(planned))
            if applies:
                say("\nApplying" + (f": {unit.title}" if unit.title else ""))
                current = False
                apply_plan(planned, sh)
                check_converged(unit.resources, unit.new_ctx())
                if applied:
                    applied()
            if unit.verify and (applies or not unit.needed_by):
                say("\nHealth checks" + (f": {unit.title}" if unit.title else ""))
                run_verify(unit.verify, unit.new_ctx())
        except Exception as e:
            if unit.title:
                sh.log.event("unit", title=unit.title, ok=False, error=str(e))
                say(f"\nStopped at {unit.title}")
                say("What was applied before it stays in place; a re-run resumes here.")
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
        say(f"ERROR: {e}")
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
    t0, status, error = time.monotonic(), "ok", None
    tty = sys.stdin.isatty() if tty is None else tty
    try:
        code = run_flow(
            recipe,
            sh,
            remove=remove,
            reconfigure=reconfigure,
            input_fn=input_fn,
            tty=tty,
        )
        status = "ok" if code == 0 else "incomplete"
    except Fail as e:
        status, error = "failed", str(e)
        say(f"\nERROR: {e}")
    except (KeyboardInterrupt, EOFError) as e:
        status, error = "aborted", repr(e)
        say("\nABORTED")
    except Exception as e:  # noqa: BLE001
        status, error = "crashed", traceback.format_exc()
        say(f"\nCRASHED: {e!r}")
    finally:
        duration = round(time.monotonic() - t0, 3)
        log.event("end", status=status, error=error, duration_s=duration)
        log.close()
        say(f"log: {log.path}")
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
    names = list(builtin_recipes())
    if not names:
        say("no recipes")
    for name in names:
        description = ""
        try:
            recipe = load_recipe(name)
            description = recipe.description
            state = recipe_state(recipe, sh)
        except Fail as e:
            state = f"error: {e}"
        say(f"  {name:<18}  {state:<30}  {description}")
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
