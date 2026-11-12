import re
import shlex
import time
import urllib.error
import urllib.request

from sux.install.log import Fail, say

HTTP_RETRY_S = 0.5


def verify_unit_active(ctx, entry):
    """`name = "x.service"` or `names = [...]`: every unit is active."""
    names = entry.get("names") or [entry["name"]]
    for name in names:
        state = ctx.sh.out(["systemctl", "is-active", name])
        if state != "active":
            hint = f"journalctl -u {name} -n 50"
            raise Fail(f"{name} is {state or 'not running'} ({hint})")
    return f"active: {', '.join(names)}"


def verify_listeners(ctx, entry):
    """`port` is open on exactly `addresses` (so not on 0.0.0.0 or the LAN)."""
    port = str(entry["port"])
    want = {f"[{a}]:{port}" if ":" in a else f"{a}:{port}" for a in entry["addresses"]}
    have = set()
    for line in ctx.sh.out(["ss", "-ltnH"]).splitlines():
        fields = line.split()
        if len(fields) > 3 and fields[3].rsplit(":", 1)[-1] == port:  # noqa: PLR2004
            have.add(re.sub(r"%[^:\]]+", "", fields[3]))  # drop %interface
    if have != want:
        raise Fail(f"port {port} listens on {sorted(have)}, expected {sorted(want)}")
    if not want:
        return f"nothing listens on port {port}"
    return f"port {port} listens only on {', '.join(sorted(want))}"


def verify_http_ok(ctx, entry):  # noqa: ARG001
    """GET `url` answers `status` (200) within `timeout` seconds (30)."""
    url, want = entry["url"], entry.get("status", 200)
    deadline = time.monotonic() + entry.get("timeout", 30)
    while True:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                got = resp.status
        except urllib.error.HTTPError as e:
            got = e.code
        except OSError as e:
            got = str(e)
        if got == want:
            return f"GET {url} -> {got}"
        if time.monotonic() > deadline:
            raise Fail(f"GET {url} -> {got}, expected {want}")
        time.sleep(HTTP_RETRY_S)


def verify_command(ctx, entry):
    """`argv` exits with `rc` (0); `contains` must appear in its output."""
    argv = entry["argv"]
    r = ctx.sh.probe(argv, sudo=entry.get("sudo", False))
    if r is None:
        raise Fail(f"{shlex.join(argv)} needs sudo")
    output = r.stdout + r.stderr
    if r.returncode != entry.get("rc", 0):
        raise Fail(f"{shlex.join(argv)} exited {r.returncode}: {output.strip()}")
    if entry.get("contains", "") not in output:
        raise Fail(f"{shlex.join(argv)}: output lacks {entry['contains']!r}")
    return shlex.join(argv)


def verify_python(ctx, entry):
    """`function` in hooks.py, called as fn(ctx, entry); raises Fail if unhealthy."""
    fn = ctx.hooks.get(entry["function"])
    if fn is None:
        raise Fail(f"hooks.py has no function {entry['function']!r}")
    return fn(ctx, entry) or entry["function"]


VERIFY = {
    "unit_active": verify_unit_active,
    "listeners": verify_listeners,
    "http_ok": verify_http_ok,
    "command": verify_command,
    "python": verify_python,
}


def run_verify(entries, ctx):
    """Run the health checks in order; the first failure stops the run."""
    for entry in entries:
        kind = entry.get("kind")
        if kind not in VERIFY:
            raise Fail(f"verify: unknown kind {kind!r} (known: {', '.join(VERIFY)})")
        try:
            message = VERIFY[kind](ctx, entry)
        except KeyError as e:
            raise Fail(f"verify {kind}: missing key {e}") from None
        except Fail as e:
            ctx.log.event("verify", kind=kind, ok=False, error=str(e))
            raise
        ctx.log.event("verify", kind=kind, ok=True, msg=message)
        say(f"  ok  {message}")
