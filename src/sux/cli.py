import argparse
import sys

from sux.config import apply_config
from sux.constants import APT_PROFILES
from sux.docker import list_sessions
from sux.git import ensure_worktree
from sux.session import attach_or_create, docker_session, kill_session
from sux.testing import config_test

MODULE_DOC = """\
sux - a tmux wrapper with sandboxing and screen keybindings

Modes:
  tmux      `sux <name>` creates or attaches to a named tmux session.
            `sux` with no args attaches to the most recent session.

  docker    `sux -d <name>` mounts the current directory into an isolated
            Docker container with rust, uv, node, and claude-code.
            Non-root user with sudo and NVIDIA GPU passthrough.
            `sux <name>` will automatically reattach if a container exists.

  worktree  `sux -w <name>` creates a git worktree in ./worktrees/<name>
            on a new branch and opens a tmux session in it.

  combined  `sux -w -d <name>` creates a worktree and runs it in Docker.

  yolo      `sux -w -y "prompt" <name>` creates a worktree, starts a
            Docker container, and runs claude --dangerously-skip-permissions.
            Requires -w for safety.

  kill      `sux -k <name>` kills the tmux session and removes the container.

  list      `sux -l` lists all tmux sessions and running Docker containers.

  config    `sux --config` writes ~/.tmux.conf and rebuilds the Docker
            base image with all profiles (gpu, go, llvm).
            `sux --config=minimal` builds without profiles.
            `sux --config=gpu` builds with only the gpu profile.
            `sux --config --apt=vim` adds literal apt packages.
"""


def main():  # noqa: C901, PLR0912, PLR0915
    parser = argparse.ArgumentParser(
        description="A tmux wrapper with sandboxing and screen keybindings",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=MODULE_DOC,
    )
    parser.add_argument("-l", "--list", action="store_true", help="List sessions")
    parser.add_argument(
        "--config",
        nargs="?",
        const="all",
        default=None,
        metavar="PROFILES",
        help="Rebuild Docker image (profiles: minimal,gpu,go,llvm; default: all)",
    )
    parser.add_argument(
        "-k",
        "--kill",
        action="store_true",
        help="Kill session (tmux and/or Docker container)",
    )
    parser.add_argument(
        "-d",
        "--docker",
        action="store_true",
        help="Run session in a Docker container",
    )
    parser.add_argument(
        "-w",
        "--worktree",
        action="store_true",
        help="Create git worktree for session",
    )
    parser.add_argument(
        "-y",
        "--yolo",
        metavar="PROMPT",
        help="Run claude --dangerously-skip-permissions with PROMPT (requires -w)",
    )
    parser.add_argument(
        "--apt",
        help="Extra apt package names for --config",
    )
    parser.add_argument("--config-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("name", nargs="?", help="Session name")

    args = parser.parse_args()

    if args.config_test:
        config_test()
        return

    if args.config is not None:
        if args.config == "all":
            profiles = list(APT_PROFILES.keys())
        elif args.config == "minimal":
            profiles = []
        else:
            profiles = [p.strip() for p in args.config.split(",") if p.strip()]
            unknown = [p for p in profiles if p not in APT_PROFILES]
            if unknown:
                parser.error(
                    f"unknown --config profile(s): {','.join(unknown)} "
                    f"(known: {','.join(APT_PROFILES)}, or 'all'/'minimal')"
                )
        apt_packages = args.apt.split(",") if args.apt else []
        apply_config(apt_extras=profiles + apt_packages)
    elif args.list:
        list_sessions()
    elif args.kill:
        if not args.name:
            parser.error("-k/--kill requires a session name")
        kill_session(args.name)
    else:
        name = args.name
        if args.yolo and not args.worktree:
            parser.error("-y/--yolo requires -w/--worktree")
        if args.yolo:
            args.docker = True
        if not name:
            if args.docker or args.worktree:
                parser.error("-d/-w/-y require a session name")
            attach_or_create()
        elif args.worktree:
            ensure_worktree(name)
            if args.docker:
                docker_session(name, yolo=args.yolo)
            else:
                attach_or_create(name)
        elif args.docker:
            docker_session(name)
        else:
            attach_or_create(name)


if __name__ == "__main__":
    sys.exit(main())
