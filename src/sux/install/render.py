"""Everything `sux install` shows on the terminal.

One frozen dataclass per kind of thing shown; `str()` of it is the finished
text, wrapped to the terminal or 80 columns (only a path or URL too long for a
line runs past that, on a line of its own) and coloured when stdout is a
terminal. The rest of the engine builds these and passes them to `say()`.
"""

import os
import re
import shutil
import sys
from dataclasses import dataclass

MAX_WIDTH = 80
DIFF_LINES = 60  # of one diff shown in the plan; the log has all of it
STYLES = {
    "ok": "32",  # green
    "change": "33",  # yellow
    "unknown": "35",  # magenta
    "FAILED": "31",  # red
    "skipped": "2",
    "warning": "33",
    "bold": "1",
    "dim": "2",
    "number": "2;3",  # dim italic
    "add": "32",
    "remove": "31",
}
ANSI = re.compile(r"\033\[[0-9;]*m")
SHELL_WORD = re.compile(r"""(?:[^\s'"]|'[^']*'|"[^"]*"|['"])+""")  # quotes stay whole
STATUS_WIDTH = len("unknown")
BESIDE = 30  # a state row's item up to this wide can have its state beside it


def colour_on():
    """Colour only for a terminal; NO_COLOR and TERM=dumb say no, FORCE_COLOR yes."""
    env = os.environ
    if env.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty() and "NO_COLOR" not in env and env.get("TERM") != "dumb"


def paint(text, style):
    """`text` in one of STYLES; unchanged when colour is off or there is no style."""
    if not (text and style and colour_on()):
        return text
    return f"\033[{STYLES[style]}m{text}\033[0m"


def visible(text):
    """How many columns `text` takes, which its colour codes do not."""
    return len(ANSI.sub("", text))


def columns():
    return min(MAX_WIDTH, shutil.get_terminal_size((MAX_WIDTH, 24)).columns)


def wrap(text, room, shell=False):
    """One line of `text` as lines of at most `room` columns, broken at spaces.

    `shell` keeps a quoted argument on one line. A word is never cut: one
    longer than `room` (a path, a URL) gets a line of its own and may run
    past it, so that it can still be copied or clicked.
    """
    lines = [""]
    for word in SHELL_WORD.findall(text) if shell else text.split(" "):
        if lines[-1] and len(lines[-1]) + 1 + len(word) > room:
            lines.append("")
        lines[-1] += (" " if lines[-1] else "") + word
    lines = [line.rstrip() for line in lines]
    return lines if lines[-1] or len(lines) == 1 else lines[:-1]


def block(lead, text, style="", shell=False):
    """`lead`, then `text` wrapped so that it continues under its own start."""
    indent = visible(lead)
    room = max(columns() - indent, 20)
    lines = [ln for part in str(text).splitlines() for ln in wrap(part, room, shell)]
    lines = lines or [""]
    leads = [lead] + [" " * indent] * (len(lines) - 1)
    pairs = zip(leads, lines, strict=True)
    return "\n".join(a + paint(b, style) for a, b in pairs)


def clip(line, room):
    """`line` cut to `room` columns, with an ellipsis where it was cut."""
    return line if len(line) <= room else line[: max(room - 1, 0)] + "…" * (room > 0)


def status_lead(status):
    return f"  {paint(status, status)}{' ' * (STATUS_WIDTH - len(status))}  "


@dataclass(frozen=True)
class Heading:
    """What a part of the output is: the plan, a stage, a recipe that is needed."""

    text: str

    def __str__(self):
        return "\n" + block("", self.text, "bold")


@dataclass(frozen=True)
class Note:
    """A sentence about what is happening."""

    text: str
    indent: int = 0
    gap: bool = False  # an empty line before it
    style: str = ""

    def __str__(self):
        return "\n" * self.gap + block(" " * self.indent, self.text, self.style)


@dataclass(frozen=True)
class Check:
    """The result of one check: ok, change, unknown, FAILED or skipped."""

    status: str
    message: str

    def __str__(self):
        return block(status_lead(self.status), self.message)


@dataclass(frozen=True)
class Row:
    """One item of the desired state, and what it is now."""

    status: str
    what: str
    have: str = ""
    reason: str = ""

    def __str__(self):
        lead = status_lead(self.status)
        first = block(lead, self.what)
        now = self.have + (f" ({self.reason})" if self.reason else "")
        room = columns() - visible(lead) - BESIDE - 2
        if not now:
            return first
        if len(self.what) <= BESIDE and len(now) <= room:
            return f"{lead}{self.what:<{BESIDE}}  {paint(now, 'dim')}"
        return first + "\n" + block(" " * (visible(lead) + 2), now, "dim")


@dataclass(frozen=True)
class Table:
    """The state of every item of a recipe, or of a stage of it."""

    rows: tuple

    def __str__(self):
        return "\n" + "\n".join(str(row) for row in self.rows)


@dataclass(frozen=True)
class Step:
    """A command of the plan, numbered (or marked + / -), with its diff."""

    n: object
    command: str
    diff: tuple = ()

    def __str__(self):
        mark = f"{self.n:>2}"
        if isinstance(self.n, int):
            mark = paint(mark, "number")
        lines = [block(f"  {mark}  ", self.command, shell=True)]
        for line in self.diff[:DIFF_LINES]:
            style = {"+": "add", "-": "remove", "@": "dim"}.get(line[:1], "")
            header = line.startswith(("--- ", "+++ "))  # names a file: kept whole
            text = line if header else clip(line, columns() - 6)
            lines.append("      " + paint(text, style))
        more = len(self.diff) - DIFF_LINES
        if more > 0:
            lines.append(block("      ", f"... {more} more lines (in the log)", "dim"))
        return "\n".join(lines)


@dataclass(frozen=True)
class Plan:
    """The commands apply would run, and how many items are still unknown."""

    steps: tuple
    unknown: int = 0

    def __str__(self):
        parts = [Heading(f"Plan ({len(self.steps)} commands)"), *self.steps]
        if not self.steps:
            parts = [] if self.unknown else [Heading("Plan: nothing to change")]
        if self.unknown:
            text = f"{self.unknown} item(s) cannot be checked without sudo"
            parts.append(Note(text, 2))
        return "\n".join(str(part) for part in parts)


@dataclass(frozen=True)
class Situation:
    """Where things stand before the prompt, so its options explain themselves."""

    commands: int
    unknown: int

    def __str__(self):
        sudo = f"{self.unknown} item(s) can only be checked with sudo"
        text = f"Nothing to change so far. {sudo}, so there is nothing to apply yet."
        if self.commands:
            text = f"{self.commands} command(s) to run"
            text += f"; {sudo}." if self.unknown else "."
        return "\n" + block("", text)


@dataclass(frozen=True)
class Options:
    """Names with what each means, in two columns."""

    pairs: tuple  # of (name, text)
    gap: bool = True  # an empty line before them

    def __str__(self):
        width = max([8, *(len(name) for name, _ in self.pairs)])
        rows = [block(f"  {name:<{width}}  ", text) for name, text in self.pairs]
        return "\n" * self.gap + "\n".join(rows)


@dataclass(frozen=True)
class Ask:
    """A question: `str()` lists its choices, `prompt` is what input() shows."""

    text: str
    kind: str = "text"
    choices: tuple = ()
    default: object = None

    @property
    def listed(self):
        return self.kind in ("choice", "multi")

    @property
    def prompt(self):
        hint = "" if self.default is None else f" [{self.default}]"
        if self.kind == "bool" and self.default is not None:
            hint = " [Y/n]" if self.default else " [y/N]"
        if self.kind == "multi":
            hint = " (numbers or names, comma separated; 'none'; Enter keeps [x])"
        return ("  choose" if self.listed else self.text) + hint + ": "

    @property
    def styled(self):
        """The prompt for a terminal: the question bold, its default dim."""
        hint = "" if self.default in (None, "") else f" [{self.default}]"
        return f"{paint(self.text, 'bold')}{paint(hint, 'dim')}: "

    def __str__(self):
        lines = [block("", f"{self.text}:")]
        for n, choice in enumerate(self.choices, 1):
            ticked = "[x] " if choice in (self.default or []) else "[ ] "
            lead = f"  {n}. {ticked if self.kind == 'multi' else ''}"
            lines.append(block(lead, choice))
        return "\n".join(lines)


@dataclass(frozen=True)
class Command:
    """A command as it is run, and (with `output`) a line it printed."""

    text: str
    output: bool = False

    def __str__(self):
        if self.output:
            return block("      | ", self.text, "dim")
        return block("  $ ", self.text, shell=True)


@dataclass(frozen=True)
class Outcome:
    """How a run ended, and where its log is."""

    status: str  # "ok", "FAILED", "ABORTED", "CRASHED", or "" to say nothing
    detail: str = ""
    log: object = None

    def __str__(self):
        lines = []
        if self.status:
            lead = paint(self.status, self.status if self.status == "ok" else "FAILED")
            lines.append("\n" + block(f"{lead}  ", self.detail))
        if self.log:
            lines.append(block("log: ", self.log, "dim"))
        return "\n".join(lines)


@dataclass(frozen=True)
class Listing:
    """The recipes there are: name and state, with the description beneath."""

    recipes: tuple  # of (name, state, description)

    def __str__(self):
        lines = []
        for name, state, description in self.recipes:
            lines.append(block(f"  {paint(f'{name:<16}', 'bold')}  ", state))
            if description:
                lines.append(block(" " * 20, description, "dim"))
        return "\n".join(lines) or "no recipes"


@dataclass(frozen=True)
class Answered:
    """A question that was answered, on one line."""

    text: str
    answer: str

    def __str__(self):
        answer = self.answer  # a path stays whole; a list of things is cut
        if " " in answer:
            answer = clip(answer, max(columns() - len(self.text) - 2, 8))
        return clip(f"{self.text}: ", columns() - 8) + paint(answer, "ok")


@dataclass(frozen=True)
class Menu:
    """A list to pick from, as it is while the cursor is on one of its rows.

    Each row is one line, so that it can be drawn over in place; a list
    longer than the screen shows the rows around the cursor. `ticked` makes
    it a list of checkboxes.
    """

    title: str
    pairs: tuple  # of (name, text)
    cursor: int = 0
    ticked: frozenset | None = None
    hint: str = ""

    def __str__(self):
        size = shutil.get_terminal_size((MAX_WIDTH, 24))
        height = max(size.lines - 4, 3)
        top = max(0, min(self.cursor - height // 2, len(self.pairs) - height))
        width = max([8, *(len(name) for name, _ in self.pairs)])
        lines = [clip(self.title, columns())] if self.title else []
        shown = list(enumerate(self.pairs))[top : top + height]
        for n, (name, text) in shown:
            box = "" if self.ticked is None else "[x] " if n in self.ticked else "[ ] "
            row = f"{'>' if n == self.cursor else ' '} {box}{name:<{width}}"
            row = clip(row if text else row.rstrip(), columns())
            rest = clip(f"  {text}", columns() - len(row)) if text else ""
            style = "bold" if n == self.cursor else ""
            lines.append(paint(row, style) + paint(rest, "dim"))
        hidden = len(self.pairs) - len(shown)
        hint = f"{hidden} more; {self.hint}".rstrip("; ") if hidden else self.hint
        if hint:
            lines.append(paint(clip(f"  {hint}", columns()), "dim"))
        return "\n".join(lines)


def for_readline(prompt):
    """`prompt` with its colour codes marked as taking no columns."""
    return ANSI.sub(lambda code: f"\001{code[0]}\002", prompt)


def erase(lines):
    """What takes the cursor up `lines` lines and clears from there down."""
    return "\r" + (f"\033[{lines}A" if lines else "") + "\033[J"
