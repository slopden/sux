import shutil
import subprocess
from pathlib import Path

from sux.constants import TMUX_CONFIG
from sux.docker import build_docker_image
from sux.tmux import tmux_running


def apply_config(apt_extras=None):
    """Write tmux config and rebuild Docker image."""
    conf_path = Path.home() / ".tmux.conf"
    backup_path = Path.home() / ".tmux.conf.bak"

    if conf_path.exists():
        shutil.copy(conf_path, backup_path)
        print(f"Backed up existing config to {backup_path}")

    conf_path.write_text(TMUX_CONFIG)
    print(f"Wrote config to {conf_path}")

    if tmux_running():
        subprocess.run(["tmux", "source-file", str(conf_path)], check=False)
        print("Reloaded tmux config")

    # Rebuild sux-base Docker image
    subprocess.run(
        ["docker", "rmi", "-f", "sux-base"], capture_output=True, check=False
    )
    print("Building sux-base Docker image...")
    build_docker_image(apt_extras=apt_extras)
    print("Rebuilt sux-base image")
