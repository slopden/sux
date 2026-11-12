"""Questions and menus on a terminal: a list to move in, a line to edit.

Only the keys and the drawing in place live here. What is shown comes from
render.py, and what an answer means from the caller's `parse`, which the
line-based prompts (no terminal, tests, pipes) use as well.
"""

import contextlib
import glob
import os
import select
import sys
from pathlib import Path

from sux.install.log import say
from sux.install.render import (
    Answered,
    Menu,
    Note,
    columns,
    erase,
    for_readline,
    visible,
)

KEYS = {
    "\x1b[A": "up",
    "\x1b[B": "down",
    "\x1bOA": "up",
    "\x1bOB": "down",
    "\r": "enter",
    "\n": "enter",
    " ": "space",
    "\x03": "ctrl-c",
    "\x04": "ctrl-d",
}
ESCAPE_S = 0.05  # how long the rest of an arrow key may take to arrive


def on_terminal(input_fn):
    """Whether to draw menus: a person at a real terminal, asked through input()."""
    if input_fn is not input or os.environ.get("TERM", "dumb") == "dumb":
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()


@contextlib.contextmanager
def raw_keys():
    """Keys as they are pressed; the terminal is put back whatever happens."""
    import termios  # noqa: PLC0415  not there on every platform
    import tty  # noqa: PLC0415

    fd = sys.stdin.fileno()
    before = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield lambda: read_key(fd)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, before)


def read_key(fd):
    """One key: a name from KEYS, or the character."""
    data = os.read(fd, 1)
    if not data:
        return "ctrl-d"
    if data == b"\x1b":  # an arrow key sends three bytes at once
        while len(data) < 3 and select.select([fd], [], [], ESCAPE_S)[0]:  # noqa: PLR2004
            data += os.read(fd, 1)
    key = data.decode(errors="replace")
    return KEYS.get(key, key)


def move(menu, key, hot):  # noqa: PLR0911
    """What `key` does to `menu`: (the menu after it, whether it is accepted)."""
    names = [name for name, _ in menu.pairs]
    last = len(names) - 1
    starts = [n for n, name in enumerate(names) if name.lower().startswith(key.lower())]
    after = [n for n in starts if n > menu.cursor] or starts

    def at(cursor, ticked=menu.ticked):
        return Menu(menu.title, menu.pairs, cursor, ticked, menu.hint)

    if key == "ctrl-c":
        raise KeyboardInterrupt
    if key == "ctrl-d":
        raise EOFError
    if key == "enter":
        return menu, True
    if key == "up" or (key == "k" and not starts):
        return at(max(menu.cursor - 1, 0)), False
    if key == "down" or (key == "j" and not starts):
        return at(min(menu.cursor + 1, last)), False
    if menu.ticked is not None and key == "space":
        return at(menu.cursor, menu.ticked ^ {menu.cursor}), False
    if menu.ticked is not None and key == "a":
        everything = frozenset(range(len(names)))
        ticked = frozenset() if menu.ticked == everything else everything
        return at(menu.cursor, ticked), False
    if len(key) == 1 and after:
        return at(after[0]), hot and len(starts) == 1
    return menu, False


def choose_from(menu, keys=None, hot=False, out=None):
    """Let the user move in `menu` until Enter; returns it as it was accepted.

    `hot` accepts a row as soon as its first letter is typed, if no other row
    starts with it. `keys()` gives the next key (default: the terminal).
    """
    out = out or sys.stdout
    with contextlib.ExitStack() as stack:
        keys = keys or stack.enter_context(raw_keys())
        drawn, done = 0, False
        try:
            while True:
                text = str(menu)
                out.write(erase(drawn) + text)
                out.flush()
                drawn = text.count("\n")
                if done:
                    break
                menu, done = move(menu, keys(), hot)
        finally:  # the list goes; what was chosen is said in one line
            out.write(erase(drawn))
            out.flush()
    return menu


def picked(menu):
    return menu.pairs[menu.cursor][0]


def complete_path(text, state):
    """readline completer: the files that start with `text`."""
    start = str(Path.home()) + text[1:] if text.startswith("~/") else text
    found = sorted(glob.glob(glob.escape(start) + "*"))  # noqa: PTH207  a prefix
    found = [f + "/" if Path(f).is_dir() else f for f in found]
    return found[state] if state < len(found) else None


def read_line(view, read=input):
    """One edited line; Tab completes file names for a path question."""
    try:
        import readline  # noqa: PLC0415  not there on every platform
    except ImportError:
        return read(view.styled)
    prompt = for_readline(view.styled)
    before = readline.get_completer(), readline.get_completer_delims()
    try:
        if view.kind == "path":
            readline.set_completer(complete_path)
            readline.set_completer_delims("\n")
            readline.parse_and_bind("tab: complete")
        return read(prompt)
    finally:
        readline.set_completer(before[0])
        readline.set_completer_delims(before[1])


def ask_terminal(view, parse, keys=None, read=input, out=None):
    """Ask one question on a terminal; returns what `parse` made of the answer.

    A list for choice, multi and bool, a line for the rest. When it is
    answered, what was drawn is replaced by one line: the question, the answer.
    """
    out = out or sys.stdout
    if view.kind == "bool":
        menu = Menu(view.text, (("yes", ""), ("no", "")), 0 if view.default else 1)
        raw = picked(choose_from(menu, keys, hot=True, out=out))
        answer = parse(raw)
    elif view.kind == "choice":
        pairs = tuple((choice, "") for choice in view.choices)
        start = view.choices.index(view.default) if view.default in view.choices else 0
        hint = "Up/Down to move, Enter to choose"
        raw = picked(
            choose_from(Menu(view.text, pairs, start, hint=hint), keys, out=out)
        )
        answer = parse(raw)
    elif view.kind == "multi":
        pairs = tuple((choice, "") for choice in view.choices)
        ticked = {n for n, c in enumerate(view.choices) if c in (view.default or [])}
        hint = "Space to tick, a for all, Enter to accept"
        menu = Menu(view.text, pairs, 0, frozenset(ticked), hint)
        menu = choose_from(menu, keys, out=out)
        raw = ", ".join(view.choices[n] for n in sorted(menu.ticked)) or "none"
        answer = parse(raw)
    else:
        raw, answer, drawn = ask_line(view, parse, read)
        out.write(erase(drawn))
    shown = raw or str(answer)
    out.write(f"{Answered(view.text, shown)}\n")
    out.flush()
    return answer


def ask_line(view, parse, read):
    """Ask until the line is valid: (what was typed, the answer, lines drawn)."""
    drawn = 0
    while True:
        raw = read_line(view, read).strip()
        drawn += (visible(view.styled) + len(raw)) // columns() + 1
        try:
            return raw, parse(raw), drawn
        except ValueError as e:
            error = str(Note(str(e), 2, style="FAILED"))
            say(error)
            drawn += error.count("\n") + 1
