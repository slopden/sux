"""What `sux install` prints: every piece fits 80 columns, colour is optional."""

import io

import pytest

from sux.install import prompt, render
from sux.install.flow import choose
from sux.install.prompt import ask_terminal, choose_from, move, picked
from sux.install.render import (
    Answered,
    Ask,
    Check,
    Command,
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
    paint,
    visible,
)

LONG_PATH = "/etc/systemd/system/" + "very-long-unit-name-" * 6 + ".service"
LONG_TEXT = "the quick brown fox jumps over the lazy dog, " * 8
IPV6 = "fd7a:115c:a1e0:ab12:4843:cd96:6258:b240"
APT = (
    "sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -q "
    "-o Dpkg::Use-Pty=0 'a quoted argument with spaces' " + "package-name " * 12
)
DIFF = ("--- a", "+++ b", "@@ -1 +1 @@", "-" + "old " * 40, "+" + "new " * 40)

WORST = [
    Heading(f"Stage 3/5  rehearsal: {LONG_TEXT}"),
    Note(LONG_TEXT, 2),
    Note(f"answers saved to {LONG_PATH}"),
    Check("ok", f"port 445 listens only on [{IPV6}]:445, 100.80.39.92:445 " * 3),
    Check("FAILED", LONG_PATH + ": " + LONG_TEXT),
    Row("change", f"file {LONG_PATH}", "root:root 0644, 58 lines", LONG_TEXT),
    Row("unknown", "secret /etc/app.env", "root:root 0600; content unknown until sudo"),
    Row("ok", "python preflight", "vmx, IOMMU on"),
    Table((Row("ok", "apt " + "pkg " * 40, "missing: " + "pkg " * 40),)),
    Step(12, APT, DIFF * 40),
    Plan((Step(1, APT), Step(2, f"sudo install -D -m 0644 {LONG_PATH}", DIFF)), 3),
    Plan((), 2),
    Plan(()),
    Situation(123, 45),
    Situation(0, 2),
    Options((("apply", LONG_TEXT), ("a-very-long-option-name", LONG_PATH))),
    Ask(LONG_TEXT, "multi", (LONG_PATH, IPV6, "smb"), [IPV6]),
    Command(APT),
    Command("E: Unable to locate package " + LONG_PATH, output=True),
    Outcome("FAILED", f"command failed (rc=100): {APT}", LONG_PATH + ".jsonl"),
    Outcome("ok", "converged", "/home/user/.local/state/sux/install/kvm.jsonl"),
    Listing((("windows-vm", "2 unknown until sudo, 12 to change", LONG_TEXT),)),
    Answered(LONG_TEXT, LONG_PATH),
    Menu(LONG_TEXT, ((LONG_PATH, LONG_TEXT), ("exit", "leave")), 0, frozenset({1})),
]


def too_wide(line, room):
    """Over `room` columns, other than for a token that cannot be broken."""
    words = render.ANSI.sub("", line).split()
    return visible(line) > room and max(map(len, words)) <= room // 2


@pytest.fixture
def narrow(monkeypatch):
    """The real limits, which the other tests' sandbox lifts for long tmp paths."""
    monkeypatch.setattr(render, "MAX_WIDTH", 80)
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)


@pytest.mark.usefixtures("narrow")
class TestWidth:
    @pytest.mark.parametrize("thing", WORST, ids=lambda t: type(t).__name__)
    @pytest.mark.parametrize("colour", ["", "1"])
    def test_nothing_is_wider_than_80_columns(self, thing, colour, monkeypatch):
        monkeypatch.setenv("FORCE_COLOR", colour)
        lines = str(thing).splitlines()
        assert not [line for line in lines if too_wide(line, 80)]
        assert not any(line != line.rstrip() for line in lines if "\033" not in line)

    def test_a_narrower_terminal_is_respected(self, monkeypatch):
        monkeypatch.setenv("COLUMNS", "50")
        for thing in WORST:
            assert not [ln for ln in str(thing).splitlines() if too_wide(ln, 50)]

    def test_text_continues_under_its_own_column(self):
        lines = str(Check("FAILED", LONG_TEXT)).splitlines()
        assert lines[0].startswith("  FAILED   the quick")
        assert all(line.startswith(" " * 11 + line.strip()[:1]) for line in lines[1:])

    def test_a_command_wraps_between_arguments(self):
        lines = str(Step(1, APT)).splitlines()
        assert lines[0].startswith("   1  sudo env")
        assert all(line.startswith("      ") for line in lines[1:])
        assert sum("'a quoted argument with spaces'" in line for line in lines) == 1
        assert " ".join(" ".join(lines).split()[1:]) == " ".join(APT.split())

    def test_a_path_is_never_cut(self):
        log = LONG_PATH + ".jsonl"
        assert str(Outcome("", log=log)) == f"log: {log}"
        row = str(Row("change", f"file {LONG_PATH}", "missing")).splitlines()
        assert row == [
            "  change   file",
            f"           {LONG_PATH}",
            "             missing",
        ]
        step = str(Step(1, f"sudo install -m 0644 /dev/stdin {LONG_PATH}"))
        assert step.splitlines()[-1] == f"      {LONG_PATH}"
        diff = str(Step(1, "write", (f"--- {LONG_PATH}", f"+++ {LONG_PATH}", "+a")))
        assert diff.splitlines()[1:3] == [
            f"      --- {LONG_PATH}",
            f"      +++ {LONG_PATH}",
        ]
        assert str(Answered("Folder", LONG_PATH)) == f"Folder: {LONG_PATH}"

    def test_a_diff_is_cut_per_line_and_in_length(self):
        lines = str(Step(1, "write x", DIFF * 40)).splitlines()
        assert lines[4].endswith("…")
        assert len(lines) == 1 + render.DIFF_LINES + 1
        assert lines[-1].strip() == "... 140 more lines (in the log)"

    def test_a_short_row_is_one_line_a_long_one_has_its_state_beneath(self):
        short = str(Row("ok", "python preflight", "vmx, IOMMU on"))
        assert short == "  ok       python preflight" + " " * 16 + "vmx, IOMMU on"
        long = str(Row("change", "file /etc/systemd/system/app.service", "missing"))
        assert long.splitlines() == [
            "  change   file /etc/systemd/system/app.service",
            "             missing",
        ]

    def test_the_words_say_the_state_without_colour(self):
        table = str(Table((Row("ok", "a"), Row("change", "b"), Row("unknown", "c"))))
        assert [line.split()[0] for line in table.split("\n")[1:]] == [
            "ok",
            "change",
            "unknown",
        ]


@pytest.mark.usefixtures("narrow")
class TestColour:
    def test_off_when_stdout_is_not_a_terminal(self):
        assert paint("ok", "ok") == "ok"
        assert "\033" not in "".join(str(thing) for thing in WORST)

    def test_on_for_a_terminal_unless_told_not_to(self, monkeypatch):
        monkeypatch.setattr(render.sys.stdout, "isatty", lambda: True)
        monkeypatch.setenv("TERM", "xterm")
        monkeypatch.delenv("NO_COLOR", raising=False)
        assert paint("ok", "ok") == "\033[32mok\033[0m"
        monkeypatch.setenv("TERM", "dumb")
        assert paint("ok", "ok") == "ok"
        monkeypatch.setenv("TERM", "xterm")
        monkeypatch.setenv("NO_COLOR", "1")
        assert paint("ok", "ok") == "ok"

    def test_force_color_turns_it_on(self, monkeypatch):
        monkeypatch.setenv("FORCE_COLOR", "1")
        assert paint("x", "FAILED") == "\033[31mx\033[0m"
        assert "\033[1m" in str(Heading("Plan"))

    def test_colour_does_not_move_anything(self, monkeypatch):
        plain = [str(thing) for thing in WORST]
        monkeypatch.setenv("FORCE_COLOR", "1")
        coloured = [str(thing) for thing in WORST]
        assert coloured != plain
        assert [render.ANSI.sub("", text) for text in coloured] == plain


def typing(*keys):
    """A key reader that plays `keys`, and fails if more are asked for."""
    left = list(keys)
    return lambda: left.pop(0)


def menu(*names, cursor=0, ticked=None):
    return Menu("Pick", tuple((name, f"does {name}") for name in names), cursor, ticked)


@pytest.mark.usefixtures("narrow")
class TestMenu:
    def run(self, shown, *keys, hot=False):
        self.out = io.StringIO()
        return choose_from(shown, typing(*keys), hot=hot, out=self.out)

    def test_arrows_and_jk_move_and_enter_accepts(self):
        shown = menu("one", "two", "three")
        assert picked(self.run(shown, "enter")) == "one"
        assert picked(self.run(shown, "down", "down", "down", "enter")) == "three"
        assert picked(self.run(shown, "j", "j", "k", "enter")) == "two"
        assert picked(self.run(shown, "up", "enter")) == "one"  # stops at the ends

    def test_a_first_letter_jumps_and_goes_round(self):
        shown = menu("one", "two", "three")
        assert picked(self.run(shown, "t", "enter")) == "two"
        assert picked(self.run(shown, "t", "t", "enter")) == "three"
        assert picked(self.run(shown, "t", "t", "t", "enter")) == "two"
        assert picked(self.run(shown, "x", "enter")) == "one"  # no such row

    def test_hot_accepts_an_unambiguous_letter_at_once(self):
        shown = menu("apply", "reprobe", "exit", cursor=2)
        assert picked(self.run(shown, "a", hot=True)) == "apply"
        assert picked(self.run(shown, "R", hot=True)) == "reprobe"
        assert picked(self.run(shown, "enter", hot=True)) == "exit"
        both = menu("edit", "exit")
        assert picked(self.run(both, "e", "enter", hot=True)) == "exit"

    def test_space_ticks_and_a_ticks_all(self):
        shown = menu("smb", "s3", "nfs", ticked=frozenset({0}))
        assert self.run(shown, "enter").ticked == {0}
        assert self.run(shown, "down", "space", "enter").ticked == {0, 1}
        assert self.run(shown, "space", "enter").ticked == set()
        assert self.run(shown, "a", "enter").ticked == {0, 1, 2}
        assert self.run(shown, "a", "a", "enter").ticked == set()

    def test_the_cursor_row_is_marked_without_colour(self):
        text = str(menu("smb", "s3", cursor=1, ticked=frozenset({0})))
        assert text.splitlines()[:3] == [
            "Pick",
            "  [x] smb       does smb",
            "> [ ] s3        does s3",
        ]

    def test_it_is_drawn_in_place_and_gone_when_accepted(self):
        self.run(menu("one", "two"), "down", "enter")
        drawn = self.out.getvalue()
        assert drawn.count("> two") == 2  # after the key, and once accepted
        assert drawn.count("\033[2A\033[J") == 3  # each redraw, and the end
        assert drawn.endswith("\033[J")

    def test_a_list_longer_than_the_screen_scrolls(self, monkeypatch):
        monkeypatch.setenv("LINES", "10")
        monkeypatch.setenv("COLUMNS", "60")
        names = [f"item{n}" for n in range(40)]
        text = str(menu(*names, cursor=30))
        lines = text.splitlines()
        assert len(lines) <= 10
        assert any(line.startswith("> item30") for line in lines)
        assert "34 more" in lines[-1]
        assert picked(self.run(menu(*names), *["down"] * 39, "enter")) == "item39"

    def test_ctrl_c_clears_the_list_and_interrupts(self):
        with pytest.raises(KeyboardInterrupt):
            self.run(menu("one", "two"), "down", "ctrl-c")
        assert self.out.getvalue().endswith("\033[J")
        with pytest.raises(EOFError):
            move(menu("one"), "ctrl-d", hot=False)

    def test_the_terminal_is_put_back_after_ctrl_c(self, monkeypatch):
        import termios  # noqa: PLC0415
        import tty  # noqa: PLC0415

        calls = []
        monkeypatch.setattr(prompt.sys.stdin, "fileno", lambda: 0, raising=False)
        monkeypatch.setattr(termios, "tcgetattr", lambda _fd: "as it was")
        monkeypatch.setattr(termios, "tcsetattr", lambda *a: calls.append(a[2]))
        monkeypatch.setattr(tty, "setcbreak", lambda _fd: calls.append("cbreak"))
        monkeypatch.setattr(prompt, "read_key", lambda _fd: "ctrl-c")
        with pytest.raises(KeyboardInterrupt):
            choose_from(menu("one"), out=io.StringIO())
        assert calls == ["cbreak", "as it was"]


@pytest.mark.usefixtures("narrow")
class TestQuestions:
    def ask(self, view, *keys, typed=(), parse=lambda raw: raw):
        self.out = io.StringIO()
        lines = list(typed)
        return ask_terminal(
            view, parse, typing(*keys), lambda _prompt: lines.pop(0), self.out
        )

    def summary(self):
        return self.out.getvalue().rsplit("\033[J", 1)[1]

    def test_choice_starts_on_the_default_and_collapses(self):
        view = Ask("Log level", "choice", ("info", "debug"), "debug")
        assert self.ask(view, "enter") == "debug"
        assert self.ask(view, "up", "enter") == "info"
        assert self.summary() == "Log level: info\n"

    def test_multi_starts_with_the_saved_answers_ticked(self):
        view = Ask("Serve over", "multi", ("smb", "s3"), ["smb"])
        assert self.ask(view, "enter") == "smb"
        assert self.ask(view, "down", "space", "enter") == "smb, s3"
        assert self.summary() == "Serve over: smb, s3\n"
        assert self.ask(view, "space", "enter") == "none"

    def test_bool_is_yes_or_no(self):
        view = Ask("Use TLS", "bool", (), False)
        assert self.ask(view, "enter") == "no"
        assert self.ask(view, "y") == "yes"
        assert self.ask(Ask("Use TLS", "bool", (), True), "enter") == "yes"

    def test_a_line_is_asked_again_until_it_is_valid(self, capsys):
        def parse(raw):
            if not raw.startswith("/"):
                raise ValueError("give an absolute path")
            return raw

        view = Ask("Folder", "path", (), None)
        assert self.ask(view, typed=["srv", "/srv"], parse=parse) == "/srv"
        assert capsys.readouterr().out == "  give an absolute path\n"
        assert self.out.getvalue() == "\r\033[3A\033[JFolder: /srv\n"

    def test_enter_takes_the_default_which_the_summary_shows(self):
        view = Ask("Run as user", "text", (), "mike")
        assert view.styled == "Run as user [mike]: "
        assert self.ask(view, typed=[""], parse=lambda raw: raw or "mike") == "mike"
        assert self.summary() == "Run as user: mike\n"

    def test_tab_completes_file_names(self, tmp_path):
        (tmp_path / "data").mkdir()
        (tmp_path / "data.txt").touch()
        start = str(tmp_path / "da")
        found = [prompt.complete_path(start, n) for n in range(3)]
        assert found == [f"{tmp_path}/data/", f"{tmp_path}/data.txt", None]


class TestFrontEnd:
    def test_scripted_input_never_gets_the_menus(self, monkeypatch):
        monkeypatch.setenv("TERM", "xterm")
        monkeypatch.setattr(prompt.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(prompt.sys.stdout, "isatty", lambda: True)
        assert prompt.on_terminal(input) is True
        assert prompt.on_terminal(lambda _prompt: "a") is False
        monkeypatch.setenv("TERM", "dumb")
        assert prompt.on_terminal(input) is False
        monkeypatch.setenv("TERM", "xterm")
        monkeypatch.setattr(prompt.sys.stdout, "isatty", lambda: False)
        assert prompt.on_terminal(input) is False

    @pytest.mark.parametrize(
        ("can_apply", "unknown", "safe"),
        [(True, 0, "exit"), (True, 2, "exit"), (False, 2, "reprobe")],
    )
    def test_the_prompt_starts_on_what_changes_nothing(
        self, can_apply, unknown, safe, monkeypatch, capsys
    ):
        seen = []

        def enter(shown, keys=None, hot=False, out=None):  # noqa: ARG001
            seen.append((picked(shown), hot))
            return shown

        monkeypatch.setattr("sux.install.flow.on_terminal", lambda _fn: True)
        monkeypatch.setattr("sux.install.flow.choose_from", enter)
        assert choose(can_apply, unknown) == safe
        assert seen == [(safe, True)]
        assert capsys.readouterr().out.strip().endswith(f"exit: {safe}")

    def test_ctrl_c_at_the_prompt_is_exit(self, monkeypatch):
        def interrupt(*_args, **_kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr("sux.install.flow.on_terminal", lambda _fn: True)
        monkeypatch.setattr("sux.install.flow.choose_from", interrupt)
        assert choose(True, 0) == "exit"
