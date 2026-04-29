import os
import re
import subprocess

from sux.constants import APT_PROFILES, SUX_DOCKERFILE
from sux.utils import host_username


def resolve_apt_extras(items):
    """Expand profile names and collect literal package names."""
    packages = []
    for item in items:
        if item in APT_PROFILES:
            packages.extend(APT_PROFILES[item])
        else:
            packages.append(item)
    return sorted(set(packages))


def prepare_dockerfile(apt_extras=None):
    """Apply apt extras and GPU gating to the Dockerfile template.

    `apt_extras=None` defaults to all profiles (the kitchen sink). Pass an
    empty list to opt into a minimal image with no extras and no GPU.
    """
    dockerfile = SUX_DOCKERFILE
    if apt_extras is None:
        apt_extras = list(APT_PROFILES.keys())
    extras = resolve_apt_extras(apt_extras) if apt_extras else []
    has_gpu = "gpu" in apt_extras

    # GPU block: keep or remove
    if has_gpu:
        dockerfile = dockerfile.replace("# GPU_BLOCK_START\n", "")
        dockerfile = dockerfile.replace("# GPU_BLOCK_END\n", "")
        dockerfile = dockerfile.replace("# GPU_ENV_START\n", "")
        dockerfile = dockerfile.replace("# GPU_ENV_END\n", "")
    else:
        dockerfile = re.sub(
            r"# GPU_BLOCK_START\n.*?# GPU_BLOCK_END\n",
            "",
            dockerfile,
            flags=re.DOTALL,
        )
        dockerfile = re.sub(
            r"# GPU_ENV_START\n.*?# GPU_ENV_END\n",
            "",
            dockerfile,
            flags=re.DOTALL,
        )

    # APT extras: replace marker with install line or remove it
    if extras:
        install_line = (
            "RUN apt-get update && apt-get install -y "
            + " ".join(extras)
            + " \\\n    && rm -rf /var/lib/apt/lists/*\n"
        )
        dockerfile = dockerfile.replace("# APT_EXTRA\n", install_line)
    else:
        dockerfile = dockerfile.replace("# APT_EXTRA\n", "")

    return dockerfile


def ensure_docker_image(apt_extras=None, force=False):
    """Build the sux-base Docker image.

    With `force=False` (default), skip the build if the image already exists.
    With `force=True`, remove and rebuild. `apt_extras=None` builds with all
    profiles (the kitchen sink).
    """
    if not force:
        result = subprocess.run(
            ["docker", "image", "inspect", "sux-base"],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return
    else:
        subprocess.run(
            ["docker", "rmi", "-f", "sux-base"], capture_output=True, check=False
        )

    print("Building sux-base Docker image...")
    dockerfile = prepare_dockerfile(apt_extras)
    subprocess.run(
        [
            "docker",
            "build",
            "-t",
            "sux-base",
            "--build-arg",
            f"UID={os.getuid()}",
            "--build-arg",
            f"GID={os.getgid()}",
            "--build-arg",
            f"USERNAME={host_username()}",
            "-",
        ],
        input=dockerfile.encode(),
        check=True,
    )
    print("Built sux-base image")


def list_sessions():
    """List tmux and docker sessions."""
    subprocess.run(["tmux", "list-sessions"], check=False)
    result = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            "name=sux-",
            "--format",
            "{{.Names}}\t{{.Status}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.stdout.strip():
        print("\nDocker containers:")
        for line in result.stdout.strip().splitlines():
            name, status = line.split("\t", 1)
            session = name.removeprefix("sux-")
            print(f"  {session}: {status}  (sux -d {session})")
