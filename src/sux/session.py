import contextlib
import json
import os
import subprocess
from pathlib import Path

from sux.docker import ensure_docker_image
from sux.git import GitState
from sux.proxy import ensure_proxy, start_proxy, stop_proxy
from sux.tmux import run_tmux, send_commands, tmux_running, wait_for_prompt
from sux.utils import host_username


def _start_with_commands(name, commands, *exec_cmd):
    """Create a detached tmux session, send commands, then attach."""
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, *exec_cmd],
        check=True,
    )
    wait_for_prompt(name)
    send_commands(name, commands)
    run_tmux("attach-session", "-t", name)


def gpu_args():
    """GPU flags for `docker run`, only when Docker has an nvidia runtime."""
    result = subprocess.run(
        ["docker", "info", "--format", "{{json .Runtimes}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        runtimes = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(runtimes, dict) or "nvidia" not in runtimes:
        return []
    return ["--gpus", "all", "--runtime=nvidia"]


def docker_session(name, commands=None, git_rw=False):
    """Mount current directory into Docker container and open session.

    `git_rw` only applies when the container is created here.
    """
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

        git = GitState(host_dir, writable=git_rw)
        container_ws = git.container_ws

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
                *gpu_args(),
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
                "sux-base",
                "sleep",
                "infinity",
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

    exec_cmd = ["docker", "exec", "-it", "-u", user, container_name, "bash", "-l"]

    # Attach to or create tmux session running docker exec
    if tmux_running():
        result = subprocess.run(
            ["tmux", "has-session", "-t", name],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            if commands:
                send_commands(name, commands)
            run_tmux("attach-session", "-t", name)
            return

    if commands:
        _start_with_commands(name, commands, *exec_cmd)
    else:
        run_tmux("new-session", "-s", name, *exec_cmd)


def attach_or_create(name=None, commands=None):
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
                if commands:
                    send_commands(name, commands)
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
            exec_cmd = [
                "docker",
                "exec",
                "-it",
                "-u",
                host_username(),
                container_name,
                "bash",
                "-l",
            ]
            if commands:
                _start_with_commands(name, commands, *exec_cmd)
            else:
                run_tmux("new-session", "-s", name, *exec_cmd)
            return

        # Fall through to new plain tmux session
        if commands:
            _start_with_commands(name, commands)
        else:
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
