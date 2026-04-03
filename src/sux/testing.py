import subprocess
import sys
import time
from pathlib import Path

from sux.docker import build_docker_image
from sux.proxy import start_proxy, stop_proxy
from sux.utils import host_username


def config_test():
    """Run automated tests: rebuild image, start container, verify proxy."""
    test_name = "sux-config-test"
    container_name = f"sux-{test_name}"
    user = host_username()
    passed = 0
    failed = 0

    def check(desc, cmd, *, expect_success=True):
        nonlocal passed, failed
        result = subprocess.run(
            ["docker", "exec", "-u", user, container_name, *cmd],
            capture_output=True,
            text=True,
            check=False,
        )
        ok = (result.returncode == 0) == expect_success
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"  [{status}] {desc}")
        if not ok:
            if result.stdout.strip():
                print(f"         stdout: {result.stdout.strip()[:200]}")
            if result.stderr.strip():
                print(f"         stderr: {result.stderr.strip()[:200]}")
        return ok

    try:
        # Rebuild image
        print("Rebuilding sux-base image...")
        subprocess.run(
            ["docker", "rmi", "-f", "sux-base"],
            capture_output=True,
            check=False,
        )
        build_docker_image()

        # Start proxy and container
        print("Starting test container...")
        proxy_sock = start_proxy(test_name)

        host_dir = str(Path.cwd().resolve())
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
                f"{host_dir}:/workspace",
                "-v",
                f"{proxy_sock}:/var/run/docker.sock",
                "-w",
                "/workspace",
                "sux-base",
                "sleep",
                "infinity",
            ],
            check=True,
            capture_output=True,
        )

        # Wait for container to be ready
        time.sleep(1)

        print("\nFunctionality tests:")
        check("docker ps works", ["docker", "ps"])
        check(
            "vulkaninfo has nvidia",
            ["bash", "-c", "vulkaninfo 2>/dev/null | grep -qi nvidia"],
        )
        check("docker run hello-world", ["docker", "run", "--rm", "hello-world"])

        print("\nSecurity tests (dangerous options stripped by proxy):")
        # --privileged stripped: container runs but SYS_ADMIN is not granted
        check(
            "--privileged stripped (not actually privileged)",
            [
                "docker",
                "run",
                "--rm",
                "--privileged",
                "alpine",
                "sh",
                "-c",
                "! cat /proc/sysrq-trigger >/dev/null 2>&1",
            ],
        )
        # bind mount /etc stripped: /mnt is empty
        check(
            "bind /etc stripped (mount absent)",
            [
                "docker",
                "run",
                "--rm",
                "-v",
                "/etc:/mnt:ro",
                "alpine",
                "sh",
                "-c",
                "! test -f /mnt/hostname",
            ],
        )
        # --pid=host stripped: only see own processes
        check(
            "--pid=host stripped (PID isolated)",
            [
                "docker",
                "run",
                "--rm",
                "--pid=host",
                "alpine",
                "sh",
                "-c",
                "test $(ls -d /proc/[0-9]* | wc -l) -lt 10",
            ],
        )
        # --network=host stripped: bridge network (no docker0 visible)
        check(
            "--network=host stripped (bridge used)",
            [
                "docker",
                "run",
                "--rm",
                "--network=host",
                "alpine",
                "sh",
                "-c",
                "! ip link show docker0 >/dev/null 2>&1",
            ],
        )

        print(f"\nResults: {passed} passed, {failed} failed")

    finally:
        print("\nCleaning up...")
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            check=False,
        )
        stop_proxy(test_name)

    sys.exit(1 if failed > 0 else 0)
