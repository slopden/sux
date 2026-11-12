import os
import shlex
import subprocess
import time

from sux.install.log import Fail, RunLog, say


class Shell:
    """Runs commands with LC_ALL=C; everything is logged.

    `sudo` is prepended for privileged commands unless already root.
    Root-only probes (read-only) return None until `sudo_probes` is set,
    which the flow does only after the user chose to use sudo.
    """

    def __init__(self, log=None):
        self.log = log or RunLog()
        self.is_root = os.geteuid() == 0
        self.sudo_probes = self.is_root

    def argv(self, cmd, sudo):
        return (["sudo"] if sudo and not self.is_root else []) + list(cmd)

    def spawn(self, argv, stdin=None, interactive=False, env=None):
        """The only place a process is started (tests replace this)."""
        env = {**os.environ, **(env or {}), "LC_ALL": "C"}
        if interactive:  # talks to the terminal (password prompts)
            return subprocess.run(argv, env=env, check=False)
        # stdin is never the terminal: apt-get gets stopped (SIGTTOU) touching
        # it from under sudo's pty. sudo itself prompts on /dev/tty.
        feed = {"stdin": subprocess.DEVNULL} if stdin is None else {"input": stdin}
        return subprocess.run(
            argv, env=env, text=True, capture_output=True, check=False, **feed
        )

    def elevate(self):
        """Ask for sudo once (one password prompt), then allow root probes."""
        if not self.is_root and self.spawn(["sudo", "-v"], interactive=True).returncode:
            raise Fail("sudo was refused")
        self.sudo_probes = True

    def run(  # noqa: PLR0913
        self,
        cmd,
        *,
        sudo=True,
        stdin=None,
        secret_stdin=False,
        check=True,
        interactive=False,
    ):
        """Run a changing command: echoed, executed, output logged."""
        argv = self.argv(cmd, sudo)
        say(f"  $ {shlex.join(argv)}")
        t0 = time.monotonic()
        r = self.spawn(argv, stdin=stdin, interactive=interactive)
        fields = {"stdout": "<interactive, not captured>"}
        if not interactive:
            if r.returncode != 0:
                for line in (r.stdout + r.stderr).splitlines():
                    say(f"      | {line}")
            shown = f"<redacted {len(stdin or '')} chars>" if secret_stdin else stdin
            fields = {
                "stdin": self.log.clip(shown),
                "stdout": self.log.clip(r.stdout),
                "stderr": self.log.clip(r.stderr),
            }
        self.log.event(
            "cmd",
            kind="change",
            argv=argv,
            rc=r.returncode,
            duration_s=round(time.monotonic() - t0, 3),
            **fields,
        )
        if check and r.returncode != 0:
            raise Fail(f"command failed (rc={r.returncode}): {shlex.join(argv)}")
        return r.returncode

    def probe(self, cmd, *, sudo=False, secret_output=False, env=None):
        """Run a read-only query. None if it needs sudo and sudo is not in use.

        `env` adds environment variables for this command; it is not logged.
        """
        if sudo and not self.sudo_probes:
            return None
        argv = self.argv(cmd, sudo)
        t0 = time.monotonic()
        try:
            r = self.spawn(argv, env=env)
        except FileNotFoundError as e:  # tool not installed: a failed probe
            r = subprocess.CompletedProcess(argv, 127, "", str(e))
        self.log.event(
            "cmd",
            kind="probe",
            argv=argv,
            rc=r.returncode,
            duration_s=round(time.monotonic() - t0, 3),
            stdout="<redacted>" if secret_output else self.log.clip(r.stdout),
            stderr=self.log.clip(r.stderr),
        )
        return r

    def out(self, cmd):
        return self.probe(cmd).stdout.strip()
