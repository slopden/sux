"""Hooks for network-folder: the parts that will not reduce to data.

Preflight checks, the interactive Samba password, and the health checks that
need a login or a signed S3 request. The engine provides Action, Probe, Fail,
say and read_file as globals.
"""

import getpass
import grp
import hashlib
import hmac
import os
import pwd
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

BINARY = "/usr/bin/versitygw"
NAME = r"[A-Za-z0-9][A-Za-z0-9_-]*"  # a share: also a unit name and an S3 key id
BUCKET = r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]"
SMB_PORTS = ("445", "139")
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
UNRESERVED = "-_.~"
HTTP_OK, HTTP_PARTIAL, HTTP_NOT_FOUND = 200, 206, 404
SMALL = 1 << 20
# Mounts of these types hold nothing of the host's disks.
VIRTUAL_FS = {
    "proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "mqueue", "hugetlbfs",
    "cgroup", "cgroup2", "securityfs", "pstore", "efivarfs", "bpf", "configfs",
    "debugfs", "tracefs", "fusectl", "binfmt_misc", "autofs", "ramfs", "nsfs",
    "rpc_pipefs",
}  # fmt: skip
_asked = {}  # what one run asked for or read: SMB passwords, S3 credentials


def _bracket(address):
    return f"[{address}]" if ":" in address else address


def _listeners(ctx, ports):
    """Listening TCP sockets on `ports`, as "address:port"."""
    found = set()
    for line in ctx.sh.out(["ss", "-ltnH"]).splitlines():
        fields = line.split()
        if len(fields) > 3 and fields[3].rsplit(":", 1)[-1] in ports:  # noqa: PLR2004
            found.add(re.sub(r"%[^:\]]+", "", fields[3]))  # drop %interface
    return found


# ---------------------------------------------------------------- steps


def preflight(ctx, step, present):  # noqa: ARG001
    """Stop before any prompt when a folder cannot be served as asked."""
    root, protocols = Path(step["root"]), step["protocols"]
    if not re.fullmatch(NAME, step["share"]):
        raise Fail(f"share name {step['share']!r}: use letters, digits, - and _")
    if not root.is_dir():
        raise Fail(f"{root} is not a directory")
    try:
        uid = pwd.getpwnam(step["user"]).pw_uid
        gid = grp.getgrnam(step["group"]).gr_gid
    except KeyError as e:
        raise Fail(f"missing user/group: {e}") from None
    interfaces = set()
    for address in step["listen"]:
        found = ctx.sh.out(["ip", "-o", "addr", "show", "to", address])
        if not found:
            raise Fail(f"{address} is not on any interface (is tailscale up?)")
        interfaces.add(found.split()[1])
    notes = [f"{step['user']}({uid}):{step['group']}({gid})"]
    notes += [f"addresses on {', '.join(sorted(interfaces))}"] if interfaces else []
    if "smb" in protocols:
        _smb_ports_free(ctx, step)
    if "s3" in protocols:
        notes.append(_s3_port_free(ctx, step))
    return Probe(True, ", ".join(notes))


def _smb_ports_free(ctx, step):
    if not step["listen"]:
        raise Fail("SMB needs at least one address to listen on")
    ours = {f"{_bracket(a)}:445" for a in ("127.0.0.1", "::1", *step["smb_listen"])}
    busy = _listeners(ctx, SMB_PORTS) - ours
    if busy:
        raise Fail(f"SMB ports are in use by something else: {sorted(busy)}")


def _s3_port_free(ctx, step):
    root, port, service = Path(step["root"]), str(step["port"]), step["service"]
    if not port.isdigit():
        raise Fail(f"S3 port {port!r} is not a number")
    names = (p.name for p in root.iterdir() if p.is_dir())
    hidden = sorted(
        name
        for name in names
        if not name.startswith(".") and not re.fullmatch(BUCKET, name)
    )
    if hidden:
        say(f"    warn: not valid S3 bucket names, will be hidden: {hidden}")
    if step["health_check"] and ctx.sh.probe(["curl", "--version"]).returncode != 0:
        raise Fail("the S3 health check needs curl (apt install curl)")
    same = [f for f in ctx.values["folder"] if "s3" in f["protocols"]]
    if sum(str(f["port"]) == port for f in same) > 1:
        raise Fail(f"more than one folder serves S3 on port {port}")
    want = {f"{_bracket(a)}:{port}" for a in (*step["listen"], "127.0.0.1")}
    busy = want & _listeners(ctx, (port,))
    if busy and ctx.sh.out(["systemctl", "is-active", service]) != "active":
        raise Fail(f"already in use by something else: {sorted(busy)}")
    return f"port {port} {'held by ' + service if busy else 'free'}"


def smb_password(ctx, step, present):
    """The Samba password of the share's user: asked for when it is set."""
    user = step["user"]
    if not present and user in step["still_used_by"]:
        return Probe(True, "kept: another folder uses it")
    if present and not ctx.once(f"smb-password:{user}"):
        return Probe(True, "as above")
    known = False
    if ctx.sh.probe(["pdbedit", "-V"]).returncode == 0:  # else: no Samba (yet)
        r = ctx.sh.probe(["pdbedit", "-L"], sudo=True)
        if r is None:
            return Probe(None, "unknown until sudo")
        known = any(ln.split(":")[0] == user for ln in r.stdout.splitlines())
    have = "set" if known else "not set"
    if known == present:
        return Probe(True, have)
    if present:
        desc = f"set the Samba password of {user} (asks for it)"
        return Probe(False, have), [
            Action(desc, ["smbpasswd", "-a", user], interactive=True)
        ]
    desc = f"remove the Samba password of {user}"
    return Probe(False, have), [Action(desc, ["smbpasswd", "-x", user])]


# ---------------------------------------------------------------- symlinks


def _escaping_symlinks(root, budget_s=5.0):
    """Symlinks under root to readable host files outside it: (path, target)."""
    root = Path(root).resolve()
    deadline = time.monotonic() + budget_s
    for folder, dirs, files in os.walk(root):  # does not follow symlinked dirs
        for name in files + dirs:
            path = Path(folder) / name
            if not path.is_symlink():
                continue
            target = path.resolve()
            outside = not target.is_relative_to(root)
            if outside and target.is_file() and os.access(target, os.R_OK):
                yield path.relative_to(root).as_posix(), str(target)
        if time.monotonic() > deadline:
            return


def _small_file(root, budget_s=5.0):
    """A small regular file under root, as a relative path; None if none."""
    deadline = time.monotonic() + budget_s
    for folder, _dirs, files in os.walk(root):
        for name in files:
            path = Path(folder) / name
            plain = path.is_file() and not path.is_symlink()
            if plain and 0 < path.stat().st_size < SMALL and not _unquotable(name):
                return path.relative_to(root).as_posix()
        if time.monotonic() > deadline:
            break
    return None


def _unquotable(path):
    return any(ch in path for ch in '"\\;')  # inside an smbclient command


# ---------------------------------------------------------------- SMB checks


def _tailnet_name(ctx, listen):
    """The MagicDNS name (not the bare hostname), else an address we listen on."""
    return ctx.values["magicdns"] or (listen[0] if listen else "127.0.0.1")


def smb_anonymous(ctx, entry):
    host = _tailnet_name(ctx, entry["listen"])
    if ctx.sh.probe(["smbclient", "-N", "-L", f"//{host}"]).returncode == 0:
        raise Fail("anonymous share listing succeeded; it should be refused")
    return "anonymous SMB access is refused"


def smb_login(ctx, entry):
    """The user can log in and list; a symlink leaving the share is not served."""
    user, root = entry["user"], entry["root"]
    unc = f"//{_tailnet_name(ctx, entry['listen'])}/{entry['share']}"
    if user not in _asked:
        if not sys.stdin.isatty():
            return f"{unc}: login not tested (no terminal to ask the password)"
        _asked[user] = getpass.getpass(f"  SMB password for {user} (to test login): ")
        ctx.log.secret(_asked[user])

    def smbclient(command):
        argv = ["smbclient", unc, "-U", user, "-c", command]
        r = ctx.sh.probe(argv, env={"PASSWD": _asked[user]})
        return r.returncode, (r.stdout + r.stderr).strip()

    rc, out = smbclient("ls")
    if rc != 0:
        raise Fail(f"login/listing failed: {out}")
    say(f"  ok  {user} can log in and list {unc}")
    escapes = (e for e in _escaping_symlinks(root) if not _unquotable(e[0]))
    link, target = next(escapes, (None, None))
    if link is None:
        return f"{unc}: no symlink to a host file outside it, containment not tested"
    plain = _small_file(root)
    if plain:  # control: a normal file downloads, so a failure below is a refusal
        _, out = smbclient(f'get "{plain}" /dev/null')
        if "NT_STATUS" in out:
            raise Fail(f"control download of {plain} failed: {out}")
    _, out = smbclient(f'get "{link}" /dev/null')
    if "NT_STATUS" not in out:
        raise Fail(f"{link} -> {target} was served: symlinks escape the share!")
    status = out.split("NT_STATUS_")[1].split()[0]
    return f"{unc}: symlink {link} -> {target} is not served ({status})"


# ---------------------------------------------------------------- S3 checks


def sigv4_headers(method, url, access, secret, region, headers=None, now=None):  # noqa: PLR0913
    """Headers that sign one body-less S3 request (AWS Signature Version 4).

    `url` is used as written: its path must be percent-encoded already.
    """
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    parts = urllib.parse.urlsplit(url)
    signed = {k.lower(): v.strip() for k, v in (headers or {}).items()}
    signed |= {
        "host": parts.netloc,
        "x-amz-content-sha256": EMPTY_SHA256,
        "x-amz-date": stamp,
    }
    names = ";".join(sorted(signed))
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = urllib.parse.urlencode(
        sorted(pairs), quote_via=urllib.parse.quote, safe=UNRESERVED
    )
    canonical = [method, parts.path or "/", query]
    canonical += [f"{name}:{signed[name]}" for name in sorted(signed)]
    canonical += ["", names, EMPTY_SHA256]
    scope = f"{stamp[:8]}/{region}/s3/aws4_request"
    digest = hashlib.sha256("\n".join(canonical).encode()).hexdigest()
    to_sign = f"AWS4-HMAC-SHA256\n{stamp}\n{scope}\n{digest}"
    key = f"AWS4{secret}".encode()
    for part in (*scope.split("/"), to_sign):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    del signed["host"]  # sent by urllib, from the same URL
    signed["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access}/{scope}, "
        f"SignedHeaders={names}, Signature={key.hex()}"
    )
    return signed


def _s3(entry, creds, method, path="/", query=None, headers=None, host="127.0.0.1"):  # noqa: PLR0913
    """One signed path-style request: (HTTP status, body)."""
    url = f"http://{host}:{entry['port']}" + urllib.parse.quote(path, safe="/-_.~")
    if query:
        url += "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
    signed = sigv4_headers(method, url, *creds, entry["region"], headers)
    request = urllib.request.Request(url, method=method, headers=signed)
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except OSError as e:
        raise Fail(f"{method} {url}: {e}") from e


def _xml(body, tag):
    """Text of every `tag` element, whatever its namespace."""
    found = ET.fromstring(body).iter()
    return [el.text or "" for el in found if el.tag.rpartition("}")[2] == tag]


def _credentials(ctx, path):
    """(access key id, secret) from the service's root-only environment file."""
    if path not in _asked:
        text = read_file(ctx, path, secret=True)
        if text is None:
            raise Fail(f"{path} cannot be read without sudo")
        env = dict(re.findall(r"^([A-Z_]+)=(.*)$", text, re.MULTILINE))
        secret = env.get("ROOT_SECRET_ACCESS_KEY")
        if not secret:
            raise Fail(f"no credentials in {path}")
        ctx.log.secret(secret)
        _asked[path] = (env.get("ROOT_ACCESS_KEY_ID", ""), secret)
    return _asked[path]


def s3_smoke(ctx, entry):
    """List the buckets on every endpoint, then list and read from each bucket."""
    creds = _credentials(ctx, entry["credentials"])
    hosts = [_bracket(a) for a in entry["listen"]] + ["127.0.0.1"]
    hosts += [ctx.values["magicdns"]] if ctx.values["magicdns"] else []
    hosts = list(dict.fromkeys(hosts))
    for host in hosts:
        t0 = time.monotonic()
        status, body = _s3(entry, creds, "GET", host=host)
        if status != HTTP_OK:
            raise Fail(f"{host}:{entry['port']}: listing buckets -> HTTP {status}")
        buckets = _xml(body, "Name")
        ms = round((time.monotonic() - t0) * 1000)
        ctx.log.event("verify", check="list_buckets", host=host, buckets=buckets, ms=ms)
    summary = []
    for bucket in buckets:
        listing = {"list-type": "2", "max-keys": "200"}
        status, body = _s3(entry, creds, "GET", f"/{bucket}", listing)
        if status != HTTP_OK:
            raise Fail(f"listing s3://{bucket} -> HTTP {status}")
        sized = zip(_xml(body, "Key"), _xml(body, "Size"), strict=True)
        key = next((key for key, size in sized if int(size) > 0), None)
        if key is None:
            summary.append(f"{bucket} (listed)")
            continue
        some = {"Range": "bytes=0-63"}
        status, body = _s3(entry, creds, "GET", f"/{bucket}/{key}", headers=some)
        if status not in (HTTP_OK, HTTP_PARTIAL):
            raise Fail(f"reading s3://{bucket}/{key} -> HTTP {status}")
        ctx.log.event("verify", check="bucket", bucket=bucket, key=key, read=len(body))
        summary.append(f"{bucket} (list + read ok)")
    where = f"S3 on {len(hosts)} endpoints (each listen address + MagicDNS name)"
    return f"{where}: {', '.join(summary) or 'no buckets'}"


def host_mounts(mountinfo):
    """{mount point: host path} for every mount backed by a real filesystem."""

    def unescape(text):
        return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), text)

    mounts = {}
    for line in mountinfo.splitlines():
        before, _, after = line.partition(" - ")
        fields = before.split()
        if after.split()[0] not in VIRTUAL_FS:
            mounts[unescape(fields[4])] = unescape(fields[3])
    return mounts


def s3_sandbox(ctx, entry):
    """Prove the symlink containment on the running service, two ways."""
    root, service = entry["root"], entry["service"]
    # 1. Its mount table (readable without sudo): every mount backed by a real
    #    filesystem is the empty root, the folder or the binary.
    pid = ctx.sh.out(["systemctl", "show", service, "-p", "MainPID", "--value"])
    try:
        mounts = host_mounts(Path(f"/proc/{pid}/mountinfo").read_text())
    except OSError as e:
        raise Fail(f"could not read the mounts of {service}: {e}") from e
    leaks = sorted(set(mounts.values()) - {entry["chroot"], root, BINARY})
    ctx.log.event("verify", check="sandbox_mounts", pid=pid, mounts=mounts, leaks=leaks)
    if leaks or mounts.get("/") != entry["chroot"]:
        raise Fail(f"sandbox not in effect: {service} mounts {mounts}")
    # 2. End to end: a symlink in a bucket to a readable host file is not served.
    seen = f"only {root} and {BINARY} from the host are visible to {service}"
    in_bucket = (e for e in _escaping_symlinks(root) if re.match(r"[^./][^/]*/", e[0]))
    link, target = next(in_bucket, (None, None))
    if link is None:
        return f"sandbox: {seen}; no escaping symlink found to test"
    creds = _credentials(ctx, entry["credentials"])
    status, _ = _s3(entry, creds, "HEAD", f"/{link}")
    if status == HTTP_OK:
        raise Fail(f"sandbox not in effect: s3://{link} served host file {target}")
    if status != HTTP_NOT_FOUND:
        raise Fail(f"escape test inconclusive: HEAD s3://{link} -> HTTP {status}")
    return f"sandbox: {seen}; symlink s3://{link} -> {target} is not served (404)"


def s3_client_help(ctx, entry):
    """One line another machine can use as it is (it holds the secret)."""
    access, secret = _credentials(ctx, entry["credentials"])
    host = _tailnet_name(ctx, [_bracket(a) for a in entry["listen"]])
    buckets = sorted(
        p.name
        for p in Path(entry["root"]).iterdir()
        if p.is_dir() and re.fullmatch(BUCKET, p.name)
    )
    return (
        f"{entry['share']} over S3 (path-style; buckets: {' '.join(buckets)}):\n"
        f"AWS_ENDPOINT_URL=http://{host}:{entry['port']} "
        f"AWS_REGION={entry['region']} AWS_ACCESS_KEY_ID={access} "
        f"AWS_SECRET_ACCESS_KEY={secret}"
    )
