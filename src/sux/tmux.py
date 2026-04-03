import os
import subprocess


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
