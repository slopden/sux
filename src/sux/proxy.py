import asyncio
import contextlib
import json
import os
import posixpath
import signal
import sys
import time
import traceback
from pathlib import Path

DOCKER_SOCK = "/var/run/docker.sock"
MAX_BODY_SIZE = 10 * 1024 * 1024  # 10MB

# Container IDs created through this proxy. Scopes all operations.
_owned_containers = set()

ALLOWED_HOSTCONFIG = {
    "Binds",
    "NetworkMode",
    "Tmpfs",
    "RestartPolicy",
    "Runtime",
    "DeviceRequests",
    "AutoRemove",
    "ShmSize",
    "Memory",
    "MemorySwap",
    "NanoCpus",
    "CpuShares",
    "CpuQuota",
}

ALLOWED_TOP_LEVEL = {
    "Image",
    "Cmd",
    "Entrypoint",
    "Env",
    "WorkingDir",
    "User",
    "ExposedPorts",
    "Labels",
    "Volumes",
    "Tty",
    "OpenStdin",
    "StdinOnce",
    "AttachStdin",
    "AttachStdout",
    "AttachStderr",
    "HostConfig",
    "NetworkingConfig",
    "Hostname",
    "StopSignal",
    "Healthcheck",
}

ALLOWED_EXEC = {
    "AttachStdin",
    "AttachStdout",
    "AttachStderr",
    "Tty",
    "Cmd",
    "Env",
    "WorkingDir",
}


def _is_owned(container_id):
    """Check if a container ID (or prefix) belongs to us."""
    return any(
        cid.startswith(container_id) or container_id.startswith(cid)
        for cid in _owned_containers
    )


def _parse_segments(path):
    """Parse URL path into segments, stripping version prefix and query."""
    segments = path.split("?")[0].strip("/").split("/")
    if segments and segments[0].startswith("v") and "." in segments[0]:
        segments = segments[1:]
    return segments


def sanitize_binds(binds, workspace_host):
    """Filter bind mounts to only allow /workspace-relative paths."""
    if not binds:
        return []
    result = []
    for bind in binds:
        parts = bind.split(":")
        src = parts[0]
        if not src.startswith("/"):
            result.append(bind)
        elif src == "/workspace" or src.startswith("/workspace/"):
            normalized = posixpath.normpath(src)
            if normalized == "/workspace" or normalized.startswith("/workspace/"):
                parts[0] = workspace_host + normalized[len("/workspace") :]
                result.append(":".join(parts))
            else:
                print(f"PROXY: blocked traversal: {bind}", file=sys.stderr)
        else:
            print(f"PROXY: blocked bind mount: {bind}", file=sys.stderr)
    return result


def sanitize_create(body, workspace_host):
    """Strip dangerous options from container create requests."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body
    clean = {k: v for k, v in data.items() if k in ALLOWED_TOP_LEVEL}
    hc = data.get("HostConfig")
    if hc and isinstance(hc, dict):
        clean_hc = {k: v for k, v in hc.items() if k in ALLOWED_HOSTCONFIG}
        if "Binds" in clean_hc:
            clean_hc["Binds"] = sanitize_binds(clean_hc["Binds"], workspace_host)
        if clean_hc.get("NetworkMode") == "host":
            clean_hc["NetworkMode"] = "bridge"
            print("PROXY: blocked NetworkMode=host", file=sys.stderr)
        clean["HostConfig"] = clean_hc
    return json.dumps(clean).encode()


def sanitize_exec(body):
    """Strip dangerous options from exec create requests."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body
    clean = {k: v for k, v in data.items() if k in ALLOWED_EXEC}
    return json.dumps(clean).encode()


def check_request(method, path, body, workspace_host):  # noqa: PLR0911, C901, PLR0912
    """Route check. Returns (allowed, rewritten_body)."""
    seg = _parse_segments(path)

    # Health / info
    if seg == ["_ping"]:
        return True, body
    if method == "GET" and seg in (["version"], ["info"]):
        return True, body

    # Images: list, inspect, pull, build
    if method == "GET" and seg == ["images", "json"]:
        return True, body
    if method == "GET" and len(seg) == 3 and seg[0] == "images" and seg[2] == "json":  # noqa: PLR2004
        return True, body
    if method == "POST" and seg == ["images", "create"]:
        return True, body
    if method == "POST" and seg == ["build"]:
        return True, body

    # Container list (docker ps)
    if method == "GET" and seg == ["containers", "json"]:
        return True, body

    # Container create — sanitize, ID tracked on response
    if method == "POST" and seg == ["containers", "create"]:
        return True, sanitize_create(body, workspace_host)

    # Owned container operations
    if len(seg) == 3 and seg[0] == "containers" and _is_owned(seg[1]):  # noqa: PLR2004
        action = seg[2]
        if method == "GET" and action == "json":
            return True, body
        if method == "POST" and action in ("start", "stop", "kill", "wait"):
            return True, body
        if method == "POST" and action == "exec":
            return True, sanitize_exec(body)

    # Exec start/resize (exec IDs are not container IDs — gated at exec create)
    if (
        method == "POST"
        and len(seg) == 3  # noqa: PLR2004
        and seg[0] == "exec"
        and seg[2] in ("start", "resize", "json")
    ):
        return True, body

    return False, body


def record_created_container(resp_body):
    """Parse container create response and track the ID."""
    try:
        data = json.loads(resp_body)
        cid = data.get("Id", "")
        if cid:
            _owned_containers.add(cid)
    except (json.JSONDecodeError, TypeError):
        pass


def proxy_serve(sock_path, workspace_host):  # noqa: C901, PLR0915
    """Docker socket filtering proxy. Runs in forked child, never returns."""

    async def read_http_head(reader):
        lines = []
        while True:
            line = await reader.readline()
            if not line:
                return None, {}, 0
            lines.append(line)
            if line == b"\r\n":
                break
        head = b"".join(lines)
        headers = {}
        for raw in lines[1:]:
            decoded = raw.decode("latin-1").strip()
            if ":" in decoded:
                k, v = decoded.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        return head, headers, int(headers.get("content-length", 0))

    async def forward_bytes(src, dst):
        try:
            while data := await src.read(65536):
                dst.write(data)
                await dst.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            with contextlib.suppress(Exception):
                dst.close()

    async def handle_client(client_r, client_w):  # noqa: C901, PLR0912, PLR0915
        try:
            while True:
                req_head, req_headers, req_cl = await read_http_head(client_r)
                if req_head is None:
                    return

                first_line = req_head.split(b"\r\n")[0].decode("latin-1")
                parts = first_line.split()
                if len(parts) < 2:  # noqa: PLR2004
                    return
                method, path = parts[0], parts[1]

                # Body size limit
                if req_cl > MAX_BODY_SIZE:
                    client_w.write(
                        b"HTTP/1.1 413 Payload Too Large\r\nContent-Length: 0\r\n\r\n"
                    )
                    await client_w.drain()
                    return

                body = b""
                if req_cl > 0:
                    body = await client_r.readexactly(req_cl)

                if req_headers.get("transfer-encoding", "").lower() == "chunked":
                    chunks = []
                    while True:
                        size_line = await client_r.readline()
                        size = int(size_line.strip(), 16)
                        if size == 0:
                            await client_r.readline()
                            break
                        chunks.append(await client_r.readexactly(size))
                        await client_r.readline()
                    body = b"".join(chunks)

                # Route check
                allowed, body = check_request(method, path, body, workspace_host)
                if not allowed:
                    client_w.write(
                        b"HTTP/1.1 403 Forbidden\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Content-Length: 52\r\n\r\n"
                        b'{"message":"Blocked by sux Docker security proxy."}'
                    )
                    await client_w.drain()
                    print(f"PROXY: blocked {method} {path}", file=sys.stderr)
                    return

                is_create = method == "POST" and "/containers/create" in path

                # Rebuild request with updated content-length
                new_headers = []
                for raw in req_head.split(b"\r\n")[1:]:
                    if not raw:
                        continue
                    low = raw.decode("latin-1").lower()
                    if low.startswith(("content-length:", "transfer-encoding:")):
                        continue
                    new_headers.append(raw)
                if body:
                    new_headers.append(f"Content-Length: {len(body)}".encode())
                new_req = (
                    f"{method} {path} HTTP/1.1\r\n".encode()
                    + b"\r\n".join(new_headers)
                    + b"\r\n\r\n"
                    + body
                )

                up_r, up_w = await asyncio.open_unix_connection(DOCKER_SOCK)
                up_w.write(new_req)
                await up_w.drain()

                resp_head, resp_headers, resp_cl = await read_http_head(up_r)
                if resp_head is None:
                    up_w.close()
                    return

                resp_first = resp_head.split(b"\r\n")[0].decode("latin-1")

                # 101 Upgrade: bidirectional forwarding (docker exec -it)
                if resp_first.startswith("HTTP/") and " 101 " in resp_first:
                    client_w.write(resp_head)
                    await client_w.drain()
                    await asyncio.gather(
                        forward_bytes(client_r, up_w),
                        forward_bytes(up_r, client_w),
                        return_exceptions=True,
                    )
                    return

                if resp_headers.get("transfer-encoding", "").lower() == "chunked":
                    client_w.write(resp_head)
                    await client_w.drain()
                    while True:
                        size_line = await up_r.readline()
                        if not size_line:
                            break
                        client_w.write(size_line)
                        await client_w.drain()
                        size = int(size_line.strip(), 16)
                        if size == 0:
                            client_w.write(await up_r.readline())
                            await client_w.drain()
                            break
                        client_w.write(await up_r.readexactly(size))
                        client_w.write(await up_r.readline())
                        await client_w.drain()
                elif resp_cl > 0:
                    resp_body = await up_r.readexactly(resp_cl)
                    client_w.write(resp_head + resp_body)
                    await client_w.drain()
                    # Track container create responses
                    if is_create and " 201 " in resp_first:
                        record_created_container(resp_body)
                else:
                    client_w.write(resp_head)
                    await client_w.drain()

                up_w.close()

        except (
            ConnectionResetError,
            BrokenPipeError,
            asyncio.IncompleteReadError,
        ):
            pass
        except Exception as e:  # noqa: BLE001
            print(f"PROXY error: {e}", file=sys.stderr)
        finally:
            with contextlib.suppress(Exception):
                client_w.close()

    async def serve():
        with contextlib.suppress(FileNotFoundError):
            os.unlink(sock_path)  # noqa: PTH108
        server = await asyncio.start_unix_server(handle_client, path=sock_path)
        os.chmod(sock_path, 0o600)  # noqa: PTH101
        print(f"PROXY: listening on {sock_path}", file=sys.stderr)
        async with server:
            await server.serve_forever()

    asyncio.run(serve())


def proxy_paths(name):
    """Return (sock, pid, log) paths for a proxy."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    base = f"{runtime_dir}/sux-proxy-{name}"
    return f"{base}.sock", f"{base}.pid", f"{base}.log"


def start_proxy(name):
    """Fork a child process running the filtering proxy."""
    sock, pidfile, logfile = proxy_paths(name)
    workspace_host = str(Path.cwd().resolve())

    pid = os.fork()
    if pid == 0:
        # Child: daemonize and run proxy
        os.setsid()
        sys.stdin.close()
        log_fd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        os.close(log_fd)
        try:
            proxy_serve(sock, workspace_host)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        os._exit(1)

    # Parent: save PID and wait for socket
    Path(pidfile).write_text(str(pid))
    for _ in range(50):
        if Path(sock).exists():
            return sock
        time.sleep(0.1)

    diag = Path(logfile).read_text()[:4096] if Path(logfile).exists() else ""
    msg = f"Proxy failed to start within 5s. log: {diag}"
    raise RuntimeError(msg)


def stop_proxy(name):
    """Stop the proxy and clean up."""
    sock, pidfile, logfile = proxy_paths(name)

    if Path(pidfile).exists():
        with contextlib.suppress(ValueError, ProcessLookupError, PermissionError):
            os.kill(int(Path(pidfile).read_text().strip()), signal.SIGTERM)

    for f in (pidfile, sock, logfile):
        Path(f).unlink(missing_ok=True)


def ensure_proxy(name):
    """Ensure proxy is running, (re)starting if needed."""
    _, pidfile, _ = proxy_paths(name)
    if Path(pidfile).exists():
        try:
            os.kill(int(Path(pidfile).read_text().strip()), 0)
        except (ValueError, ProcessLookupError):
            pass
        else:
            return
    start_proxy(name)
