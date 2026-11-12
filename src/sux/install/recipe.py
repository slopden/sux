import ast
import hashlib
import operator
import re
import string
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from sux.install.log import Fail, say
from sux.install.model import KINDS, Action, Probe
from sux.install.resources import (
    SECRET_MARK,
    DaemonReloadRes,
    UnitRes,
    read_file,
    unit_for,
)

RECIPES_DIR = Path(__file__).resolve().parent / "recipes"
BUILTIN_RECIPES = None  # the single-file build replaces this with the trees
WHOLE_VAR = re.compile(r"\$(\w+)|\$\{(\w+)\}")
QUESTION_KINDS = ("text", "path", "bool", "choice", "multi")
COMPARE = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}


# ---------------------------------------------------------------- loading


def read_tree(path):
    """Every file of a recipe directory as {relative path: text}."""
    files = sorted(p for p in Path(path).rglob("*") if p.is_file())
    return {
        p.relative_to(path).as_posix(): p.read_text()
        for p in files
        if "__pycache__" not in p.parts
    }


def builtin_recipes():
    if BUILTIN_RECIPES is not None:
        return BUILTIN_RECIPES
    if not RECIPES_DIR.is_dir():
        return {}
    dirs = sorted(d for d in RECIPES_DIR.iterdir() if (d / "recipe.toml").is_file())
    return {d.name: read_tree(d) for d in dirs}


@dataclass
class Recipe:
    name: str
    tree: dict  # {relative path: text}: recipe.toml, templates/*, hooks.py
    data: dict  # parsed recipe.toml
    sha256: str
    source: Path | None = None  # its directory, when it was loaded by path
    _hooks: dict | None = field(default=None, repr=False)

    @property
    def description(self):
        return self.data.get("recipe", {}).get("description", "")

    @property
    def variables(self):
        return self.data.get("vars", {})

    @property
    def instances(self):
        """`[instances]`: what the recipe manages a list of, or {}.

        `name` is the list (`[[<name>]]` in the answers file, `each =
        "<name>"` on steps), `vars` the questions asked per instance, and
        `key` the variable that tells instances apart.
        """
        return self.data.get("instances", {})

    @property
    def needs(self):
        """`needs = ["<recipe>"]`: recipes that are applied before this one."""
        return list(self.data.get("recipe", {}).get("needs", []))

    @property
    def stages(self):
        """`[[stage]]` tables (`name`, `title`, optional `confirm`), in order.

        A step or verify entry belongs to the stage it names with `stage =
        "<name>"`. Stages are applied one after the other, each verified
        before the next one starts.
        """
        return self.data.get("stage", [])

    def template(self, name):
        try:
            return self.tree[f"templates/{name}"]
        except KeyError:
            raise Fail(f"recipe {self.name}: no template {name!r}") from None

    def hooks(self):
        """Public functions of hooks.py, which may use the names given here."""
        if self._hooks is None:
            self._hooks = self._load_hooks()
        return self._hooks

    def _load_hooks(self):
        source = self.tree.get("hooks.py")
        if not source:
            return {}
        scope = {
            "Action": Action,
            "Probe": Probe,
            "Fail": Fail,
            "say": say,
            "read_file": read_file,
        }
        exec(compile(source, f"{self.name}/hooks.py", "exec"), scope)
        return {k: v for k, v in scope.items() if callable(v) and not k.startswith("_")}


def load_recipe(ref):
    """Load a built-in recipe by name, or any recipe directory by path."""
    source = None
    if "/" in str(ref):
        if not (Path(ref) / "recipe.toml").is_file():
            raise Fail(f"no recipe.toml in {ref}")
        tree, source = read_tree(ref), Path(ref)
    else:
        known = builtin_recipes()
        if ref not in known:
            names = ", ".join(known) or "none"
            raise Fail(f"unknown recipe {ref!r} (available: {names})")
        tree = known[ref]
    try:
        data = tomllib.loads(tree["recipe.toml"])
    except tomllib.TOMLDecodeError as e:
        raise Fail(f"{ref}/recipe.toml: {e}") from e
    name = data.get("recipe", {}).get("name") or Path(ref).name
    digest = hashlib.sha256(repr(sorted(tree.items())).encode()).hexdigest()
    recipe = Recipe(name, tree, data, digest, source)
    check_stages(recipe)
    return recipe


def check_stages(recipe):
    """Every step and verify entry names a declared stage, if there are any."""
    names = [stage.get("name") for stage in recipe.stages]
    if len(set(names)) != len(names) or not all(names):
        raise Fail(f"recipe {recipe.name}: every [[stage]] needs its own name")
    for table in ("step", "verify"):
        for n, raw in enumerate(recipe.data.get(table, []), 1):
            stage = raw.get("stage")
            if stage not in names if names else stage is not None:
                where = f"recipe {recipe.name}, {table} {n}"
                known = ", ".join(names) or "none declared"
                raise Fail(f"{where}: stage = {stage!r} (stages: {known})")


def sibling(recipe, name):
    """The recipe `name` as seen from `recipe`: next to it, else built in."""
    beside = recipe.source.parent / name if recipe.source else None
    if beside is not None and (beside / "recipe.toml").is_file():
        return load_recipe(beside)
    return load_recipe(name)


def dependencies(recipe, _above=()):
    """Every recipe `recipe` needs, each before what needs it, each once."""
    out = []
    for name in recipe.needs:
        if name in (*_above, recipe.name):
            chain = " -> ".join([*_above, recipe.name, name])
            raise Fail(f"recipes need each other in a circle: {chain}")
        dep = sibling(recipe, name)
        for found in [*dependencies(dep, (*_above, recipe.name)), dep]:
            if found.name not in [r.name for r in out]:
                out.append(found)
    return out


def dependents(recipe):
    """The recipes, built in or next to `recipe`, that need it."""
    names = list(builtin_recipes())
    if recipe.source:
        dirs = sorted(recipe.source.parent.iterdir())
        names += [d.name for d in dirs if (d / "recipe.toml").is_file()]
    out = []
    for name in dict.fromkeys(names):
        try:
            other = sibling(recipe, name)
            needed = [dep.name for dep in dependencies(other)]
        except Fail:
            continue  # a broken recipe is not a reason to keep this one
        if other.name != recipe.name and recipe.name in needed:
            out.append(other)
    return out


# ---------------------------------------------------------------- values


class Values(dict):
    """Recipe variables by name; names that are not variables fall back to facts."""

    def __init__(self, facts, hooks=None):
        super().__init__()
        self.facts = facts
        self.hooks = hooks or {}  # hooks.py functions, for `hook` variables
        self.gone = {}  # {list name: instances removed from the answers}

    def __missing__(self, key):
        return self.facts.get(key)

    def child(self, **extra):
        scope = Values(self.facts, self.hooks)
        scope.gone = self.gone
        scope.update(self)
        scope.update(extra)
        return scope


class _Formatted:
    """Mapping view that string.Template reads from."""

    def __init__(self, values):
        self.values = values

    def __getitem__(self, key):
        value = self.values[key]
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (list, tuple)):
            return " ".join(str(v) for v in value)
        return str(value)


def render(text, values, where):
    """Substitute $name / ${name}; `$$` is a literal dollar sign."""
    try:
        return string.Template(text).substitute(_Formatted(values))
    except KeyError as e:
        raise Fail(f"{where}: unknown variable ${e.args[0]}") from None
    except ValueError as e:
        raise Fail(f"{where}: {e} (write $$ for a literal $)") from None


def expand(obj, values, where):
    """Render every string inside a TOML value.

    A string that is exactly `$name` becomes that variable's value with its
    type kept; when that is a list inside a list, its items are spliced in.
    """
    if isinstance(obj, str):
        whole = WHOLE_VAR.fullmatch(obj)
        if not whole:
            return render(obj, values, where)
        try:
            return values[whole[1] or whole[2]]
        except KeyError:
            raise Fail(f"{where}: unknown variable {obj}") from None
    if isinstance(obj, dict):
        return {k: expand(v, values, where) for k, v in obj.items()}
    if isinstance(obj, list):
        out = []
        for item in obj:
            value = expand(item, values, where)
            splice = isinstance(item, str) and isinstance(value, list)
            out += value if splice else [value]
        return out
    return obj


def evaluate(expr, values):  # noqa: C901
    """Evaluate a `when` expression over the values, without eval().

    Supports names, literals, lists, `in` / `not in`, `==` / `!=`, `not`,
    `and` / `or`.
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise Fail(f"bad expression {expr!r}: {e.msg}") from e

    def walk(node):  # noqa: C901, PLR0911
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            try:
                return values[node.id]
            except KeyError:
                raise Fail(f"{expr!r}: unknown name {node.id!r}") from None
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return [walk(e) for e in node.elts]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not walk(node.operand)
        if isinstance(node, ast.BoolOp):
            results = (walk(v) for v in node.values)
            return all(results) if isinstance(node.op, ast.And) else any(results)
        if isinstance(node, ast.Compare):
            left = walk(node.left)
            for op, comparator in zip(node.ops, node.comparators, strict=True):
                right = walk(comparator)
                if type(op) not in COMPARE:
                    break
                if not COMPARE[type(op)](left, right):
                    return False
                left = right
            else:
                return True
        raise Fail(f"{expr!r}: only names, literals, in, not, and, or, ==, != work")

    try:
        return walk(tree.body)
    except TypeError as e:
        raise Fail(f"{expr!r}: {e}") from e


# ---------------------------------------------------------------- questions


@dataclass
class Question:
    name: str
    text: str
    kind: str
    choices: list
    default: object  # None: there is no default, an answer is needed


def default_for(spec, values, where):
    """A variable's value before any answer: `default`, `hook` or `fact`.

    `hook = "<function>"` is a hooks.py function called with the values worked
    out so far; it returns the value (for what a recipe has to compute).
    """
    if "default" in spec:
        return expand(spec["default"], values, where)
    if "hook" in spec:
        fn = values.hooks.get(spec["hook"])
        if fn is None:
            raise Fail(f"{where}: hooks.py has no function {spec['hook']!r}")
        return fn(values)
    if "fact" in spec:
        try:
            return values.facts.get(spec["fact"])
        except KeyError:
            raise Fail(f"{where}: unknown fact {spec['fact']!r}") from None
    return None


def question_for(name, spec, values, saved):
    where = f"var {name}"
    kind = spec.get("kind", "text")
    if kind not in QUESTION_KINDS:
        raise Fail(f"{where}: kind must be one of {QUESTION_KINDS}")
    default = default_for(spec, values, where)
    choices = expand(spec.get("choices", []), values, where)
    if kind in ("choice", "multi") and not choices:
        # offered straight from a fact, e.g. the addresses this host has
        if not isinstance(default, list):
            raise Fail(f"{where}: needs `choices`, or a fact that is a list")
        choices, default = default, default if kind == "multi" else None
    return Question(name, spec["ask"], kind, choices, saved.get(name, default))


def fill(specs, values, saved, ask=None, reconfigure=False):
    """Work out the variables `specs` into `values`.

    Returns (answers, unanswered). A question whose `when` is false is not
    asked and takes its default.
    """
    answers, unanswered = {}, []
    for name, spec in specs.items():
        where = f"var {name}"
        if not isinstance(spec, dict):
            values[name] = expand(spec, values, where)
        elif "ask" not in spec or not evaluate(spec.get("when", "True"), values):
            values[name] = default_for(spec, values, where)
        elif name in saved and not reconfigure:
            values[name] = answers[name] = saved[name]
        else:
            question = question_for(name, spec, values, saved)
            if ask is None:
                unanswered.append(question)
                values[name] = question.default
            else:
                values[name] = answers[name] = ask(question)
    return answers, unanswered


def collect(spec, values, name):
    """`{ collect = "<var>", when = "<expr>" }`: one list from every instance.

    That variable of each instance for which `when` holds, with lists
    flattened and duplicates dropped.
    """
    out = []
    for instance in values[name]:
        if evaluate(spec.get("when", "True"), values.child(**instance)):
            found = instance[spec["collect"]]
            found = found if isinstance(found, list) else [found]
            out += [v for v in found if v not in out]
    return out


def resolve_instances(spec, values, saved, ask, menu):
    """Fill `values` with the instances; returns (answer rows, unanswered).

    Each `[[<name>]]` table of the answers is one instance. A row with
    `removed = true` is kept apart in `values.gone` so that what it owned can
    be planned for removal. `menu(rows, one)` edits the rows interactively.
    """
    name, key = spec["name"], spec["key"]

    def one(row, asking=False):
        """An instance's (variables, answers, unanswered questions)."""
        scope = values.child()
        got, missing = fill(spec["vars"], scope, row, ask if asking else None, asking)
        return {n: scope[n] for n in spec["vars"]}, got, missing

    rows = saved.get(name, [])
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise Fail(f"answers: {name} must be a list of tables ([[{name}]])")
    if menu is not None:
        rows = menu(rows, one)
    values[name], values.gone[name] = [], []
    answers, unanswered = [], []
    for row in rows:
        instance, got, missing = one(row)
        removed = bool(row.get("removed"))
        (values.gone if removed else values)[name].append(instance)
        answers.append(got | ({"removed": True} if removed else {}))
        unanswered += missing
    keys = [row.get(key) for row in answers]
    twice = sorted({str(k) for k in keys if keys.count(k) > 1})
    if twice:
        raise Fail(f"answers: more than one {name} has {key} = {', '.join(twice)}")
    if not rows:
        unanswered += one({})[2]
    return answers, unanswered


def resolve(  # noqa: PLR0913
    recipe, facts, saved, ask=None, reconfigure=False, menu=None
):
    """Work out every variable, asking the questions that have no saved answer.

    Returns (values, answers, unanswered). Without `ask`, questions that still
    need an answer are returned instead of asked. `collect` variables come
    last, after the instances they read.
    """

    def collects(spec):
        return isinstance(spec, dict) and "collect" in spec

    values = Values(facts, recipe.hooks())
    plain = {n: s for n, s in recipe.variables.items() if not collects(s)}
    answers, unanswered = fill(plain, values, saved, ask, reconfigure)
    spec = recipe.instances
    if spec:
        name = spec["name"]
        answers[name], missing = resolve_instances(spec, values, saved, ask, menu)
        unanswered += missing
        for var, how in recipe.variables.items():
            if collects(how):
                values[var] = collect(how, values, name)
    return values, answers, unanswered


def check_requires(recipe, facts):
    """`requires = { systemd = ">=257", arch = ["amd64"] }` against the facts."""
    for name, want in recipe.data.get("recipe", {}).get("requires", {}).items():
        have = facts.get(name)
        if isinstance(want, list):
            met = have in want
        elif isinstance(want, str) and want.startswith(">="):
            met = int(have) >= int(want[2:])
        else:
            met = have == want
        if not met:
            raise Fail(f"recipe {recipe.name} requires {name} {want}; this is {have}")


# ---------------------------------------------------------------- steps


def scopes(raw, values, where):
    """The scopes a step, fragment or verify entry is built in: [(scope, alive)].

    Without `each` that is the values as they are. `each = "<list var>"`
    repeats it per item: an instance's variables by name, any other item as
    `$item` (and `$host`: the same, in brackets when it is an IPv6 address,
    to write host:port). Instances removed from the answers come last, not
    alive.
    """
    name = raw.get("each")
    if name is None:
        return [(values, True)]

    def scope(item):
        if isinstance(item, dict):
            return values.child(**item)
        host = f"[{item}]" if ":" in str(item) else item
        return values.child(item=item, host=host)

    live = expand(f"${name}", values, where)
    if not isinstance(live, list):
        raise Fail(f"{where}: each = {name!r} is not a list")
    gone = values.gone.get(name, [])
    return [(scope(i), True) for i in live] + [(scope(i), False) for i in gone]


def render_template(recipe, step, values, where):
    """Render a step's template, with its fragments filled in first.

    A fragment is its own template file (or an inline `text`), placed where
    the main template says `$<name>`. `when` leaves it out; `each` repeats it
    per item (see `scopes`), with `when` checked per item. A placeholder alone
    on a line vanishes with its line when the fragment is empty.
    """
    text = recipe.template(step["template"])
    filled = values.child()
    for frag in step.get("fragments", []):
        name = frag["name"]
        inline = "text" in frag
        body = frag["text"] + "\n" if inline else recipe.template(frag["template"])
        parts = [
            render(body, scope, where)
            for scope, alive in scopes(frag, values, where)
            if alive and evaluate(frag.get("when", "True"), scope)
        ]
        filled[name] = "".join(parts).removesuffix("\n")
        if not parts:
            alone = rf"^[ \t]*\$(?:{name}|\{{{name}\}})[ \t]*\n"
            text = re.sub(alone, "", text, flags=re.MULTILINE)
    return render(text, filled, where)


def build_step(recipe, raw, values, present, where):
    """One resource from a step, with its strings rendered in `values`."""
    kind = raw["kind"]
    scope = values.child(secret=SECRET_MARK) if kind == "secret" else values
    skip = ("when", "keep", "cleanup", "each", "template", "fragments", "stage")
    step = expand({k: v for k, v in raw.items() if k not in skip}, scope, where)
    try:
        if "template" in raw:
            step["content"] = render_template(recipe, raw, scope, where)
        return KINDS[kind](step, present)
    except KeyError as e:
        raise Fail(f"{where}: missing key {e}") from None


def build_resources(recipe, values, remove=False, stage=None):
    """Recipe steps as resources; with `stage`, only that stage's steps.

    A step whose `when` is false (or every step, with `remove`) is built with
    the inverse target so anything it left behind is planned for removal;
    `keep = true` exempts a step from that, and `cleanup = true` is a step
    that only ever removes (it names a leftover). `each` builds the step once per
    item (see `scopes`); for an instance removed from the answers that is the
    inverse target too. Removals run first, last step first; the rest follows
    in file order.
    """
    wanted, unwanted = [], []
    for n, raw in enumerate(recipe.data.get("step", []), 1):
        if stage is not None and raw.get("stage") != stage:
            continue
        kind = raw.get("kind")
        where = f"recipe {recipe.name}, step {n} ({kind})"
        if kind not in KINDS:
            raise Fail(f"{where}: unknown kind (known: {', '.join(KINDS)})")
        for scope, alive in scopes(raw, values, where):
            wants = alive and bool(evaluate(raw.get("when", "True"), scope))
            present = not remove and wants
            if raw.get("cleanup" if present else "keep"):
                continue
            resource = build_step(recipe, raw, scope, present, where)
            (wanted if present else unwanted).append(resource)
    return with_reloads([*reversed(unwanted), *wanted])


def unit_file(resource):
    return resource.kind == "file" and unit_for(resource.target)


def managed_units(resources):
    """Every systemd unit the resources name: unit states and unit files."""
    units = [r.name for r in resources if isinstance(r, UnitRes)]
    units += [unit_for(r.target) for r in resources if unit_file(r)]
    return list(dict.fromkeys(units))


def with_reloads(resources):
    """Add `systemctl daemon-reload` where unit files changed before it."""
    units = managed_units(resources)
    if not units:
        return resources
    started = [isinstance(r, UnitRes) and r.present for r in resources]
    starts = [i for i, yes in enumerate(started) if yes]
    out = list(resources)
    if not starts or any(unit_file(r) for r in resources[starts[0] :]):
        out.append(DaemonReloadRes({"units": units, "label": "daemon-reload (last)"}))
    if starts:
        out.insert(starts[0], DaemonReloadRes({"units": units}))
    return out


def build_verify(recipe, values, stage=None):
    """The verify entries that apply (of one stage), with their strings rendered."""
    return build_entries(recipe, values, "verify", stage)


def build_status(recipe, values):
    """The recipe's own `[[status]]` commands (`argv`, optional `sudo`)."""
    return build_entries(recipe, values, "status")


def build_entries(recipe, values, table, stage=None):
    entries = []
    for n, raw in enumerate(recipe.data.get(table, []), 1):
        if stage is not None and raw.get("stage") != stage:
            continue
        where = f"recipe {recipe.name}, {table} {n} ({raw.get('kind', 'command')})"
        body = {k: v for k, v in raw.items() if k not in ("when", "each", "stage")}
        for scope, alive in scopes(raw, values, where):
            if alive and evaluate(raw.get("when", "True"), scope):
                entries.append(expand(body, scope, where))
    return entries
