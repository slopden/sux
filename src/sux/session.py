import contextlib
import os
import subprocess
from pathlib import Path

from sux.docker import ensure_docker_image
from sux.git import GitState
from sux.proxy import ensure_proxy, start_proxy, stop_proxy
from sux.tmux import run_tmux, tmux_running
from sux.utils import host_username


def _mask_mounts(host_dir, container_ws):
    """Build --tmpfs args to hide host caches/secrets from the container."""
    mounts = []
    if (Path(host_dir) / "secrets").is_dir():
        mounts += ["--tmpfs", f"{container_ws}/secrets"]
    for child in Path(host_dir).iterdir():
        if (
            child.is_dir()
            and child.name.startswith(".")
            and child.name not in (".git", ".github", ".claude")
        ):
            mount = f"{container_ws}/{child.name}"
            # .venv needs exec (python binaries) and extra size for deps
            if child.name == ".venv":
                mount += ":exec,size=2g"
            mounts += ["--tmpfs", mount]
    return mounts


def docker_session(name, yolo=None):
    """Mount current directory into Docker container and open session."""
    container_name = f"sux-{name}"
    host_dir = str(Path.cwd().resolve())
    claude_dir = str(Path.home() / ".claude")
    claude_json = str(Path.home() / ".claude.json")
    user = host_username()

    ensure_docker_image()

    # Check if container exists
    result = subprocess.run(
        ["docker", "container", "inspect", container_name],
        capture_output=True,
        check=False,
    )

    if result.returncode != 0:
        # Start filtering proxy
        proxy_sock = start_proxy(name)

        git = GitState(host_dir)
        container_ws = git.container_ws

        secrets_mounts = _mask_mounts(host_dir, container_ws)

        # Pass through auth environment variables
        env_args = []
        for key in ("ANTHROPIC_API_KEY",):
            val = os.environ.get(key)
            if val:
                env_args += ["-e", f"{key}={val}"]

        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container_name,
                "--gpus",
                "all",
                "--runtime=nvidia",
                "-v",
                f"{host_dir}:{container_ws}",
                *git.git_mounts,
                "-v",
                f"{claude_dir}:/home/{user}/.claude",
                "-v",
                f"{claude_json}:/home/{user}/.claude.json",
                "-v",
                f"{proxy_sock}:/var/run/docker.sock",
                "-w",
                container_ws,
                *env_args,
                *secrets_mounts,
                "sux-base",
                *git.container_cmd,
            ],
            check=True,
        )
        print(f"Started container: {container_name}")
    else:
        ensure_proxy(name)
        subprocess.run(
            ["docker", "start", container_name],
            capture_output=True,
            check=False,
        )

    # Attach to or create tmux session running docker exec
    if tmux_running():
        result = subprocess.run(
            ["tmux", "has-session", "-t", name],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            run_tmux("attach-session", "-t", name)
            return

    if yolo:
        run_tmux(
            "new-session",
            "-s",
            name,
            "docker",
            "exec",
            "-it",
            "-u",
            user,
            container_name,
            "bash",
            "-lc",
            f'yolo "{yolo}"',
        )
    else:
        run_tmux(
            "new-session",
            "-s",
            name,
            "docker",
            "exec",
            "-it",
            "-u",
            user,
            container_name,
            "bash",
            "-l",
        )


def attach_or_create(name=None):
    """Attach to session, creating if needed."""
    if name:
        # Check for existing tmux session first
        if tmux_running():
            result = subprocess.run(
                ["tmux", "has-session", "-t", name],
                capture_output=True,
                check=False,
            )
            if result.returncode == 0:
                run_tmux("attach-session", "-t", name)
                return

        # Check for existing docker container
        container_name = f"sux-{name}"
        result = subprocess.run(
            ["docker", "container", "inspect", container_name],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            with contextlib.suppress(RuntimeError):
                ensure_proxy(name)
            subprocess.run(
                ["docker", "start", container_name],
                capture_output=True,
                check=False,
            )
            run_tmux(
                "new-session",
                "-s",
                name,
                "docker",
                "exec",
                "-it",
                "-u",
                host_username(),
                container_name,
                "bash",
                "-l",
            )
            return

        # Fall through to new plain tmux session
        run_tmux("new-session", "-s", name)
    # Attach to most recent, or create new
    elif tmux_running():
        run_tmux("attach-session")
    else:
        run_tmux("new-session")


def kill_session(name):
    """Kill a tmux session and/or Docker container."""
    killed = False
    if tmux_running():
        result = subprocess.run(
            ["tmux", "kill-session", "-t", name],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            print(f"Killed tmux session: {name}")
            killed = True

    container_name = f"sux-{name}"
    result = subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        print(f"Removed container: {container_name}")
        killed = True

    # Clean up proxy
    stop_proxy(name)

    if not killed:
        print(f"No session or container found: {name}")
