import os
import subprocess
import time
import uuid


def run_tmux(*args):
    """Run tmux command, replacing current process."""
    os.execvp("tmux", ["tmux", *list(args)])


def tmux_running():
    """Check if tmux server is running."""
    return (
        subprocess.run(
            ["tmux", "list-sessions"], capture_output=True, check=False
        ).returncode
        == 0
    )


def wait_for_prompt(target, timeout=30.0, poll=0.1):
    """Poll a tmux pane until its last non-empty line looks like a shell prompt.

    Useful before `send_commands` when the pane runs a slow starter like
    `docker exec` — typing into the pty before the inner shell is reading
    can drop characters or land mid-startup.

    Returns True if a prompt appeared, False on timeout.
    """
    deadline = time.monotonic() + timeout
    while True:
        time.sleep(poll)
        out = subprocess.run(
            ["tmux", "capture-pane", "-t", target, "-p"],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode == 0:
            lines = [line.rstrip() for line in out.stdout.splitlines() if line.strip()]
            if lines and lines[-1].rstrip().endswith(("$", "#", ">", "%")):
                return True
        if time.monotonic() > deadline:
            print(f"sux: timed out waiting for shell prompt in {target}")
            return False


def send_commands(target, commands, timeout=60.0, poll=0.1, min_wait=0.5):
    """Type shell commands into a tmux pane one at a time, waiting for each.

    Each ;-delimited command ends with an implicit Enter. By default we wrap
    the command in `eval` and append a unique sentinel (`; echo __SUX_DONE__`),
    then poll `capture-pane` until the sentinel appears twice — once from
    the echoed line, once from the actual output — signalling that the
    shell has returned to a prompt. The eval wrap means bash parse errors
    surface at runtime instead of swallowing the sentinel.

    If a command contains the literal token `\\n`, we treat the whole
    segment as raw keystrokes for an interactive program: type each piece
    and press Enter, with no eval wrap and no sentinel (since the shell
    prompt won't reappear while the interactive program is running).

    target: tmux target (session name or `session:window.pane`)
    commands: iterable of shell command strings
    timeout: max seconds to wait for any single command's sentinel
    poll: seconds between capture-pane checks
    min_wait: minimum seconds before declaring a command done (or, in raw
              mode, before moving to the next segment)
    """
    for i, cmd in enumerate(commands):
        if "\\n" in cmd:
            pieces = cmd.split("\\n")
            # A trailing `\n` becomes an empty final piece — drop it so
            # "claude\n" sends one Enter, not two.
            if pieces and pieces[-1] == "":
                pieces = pieces[:-1]
            for piece in pieces:
                subprocess.run(
                    ["tmux", "send-keys", "-t", target, piece, "Enter"],
                    check=True,
                )
            time.sleep(min_wait)
            continue

        sentinel = f"__SUX_DONE_{uuid.uuid4().hex[:8]}_{i}__"
        escaped = cmd.replace("'", "'\\''")
        line = f"eval '{escaped}' ; echo {sentinel}"
        subprocess.run(
            ["tmux", "send-keys", "-t", target, line, "Enter"],
            check=True,
        )
        start = time.monotonic()
        deadline = start + timeout
        floor = start + min_wait
        while True:
            time.sleep(poll)
            now = time.monotonic()
            out = subprocess.run(
                ["tmux", "capture-pane", "-t", target, "-p", "-S", "-2000"],
                capture_output=True,
                text=True,
                check=False,
            )
            done = out.returncode == 0 and out.stdout.count(sentinel) >= 2  # noqa: PLR2004
            if done and now >= floor:
                break
            if now > deadline:
                print(f"sux: timed out waiting for command: {cmd}")
                break
