"""Integration tests for the Docker socket proxy.

Requires Docker running. Skipped automatically if not available.
"""

import asyncio
import contextlib
import json
import os
import signal
import time
import uuid
from pathlib import Path

import pytest

from sux.proxy import proxy_serve

DOCKER_SOCK = "/var/run/docker.sock"
DOCKER_AVAILABLE = Path(DOCKER_SOCK).exists()
pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason="Docker not available")


def _force_remove(cid):
    """Remove a container directly via the real Docker socket."""

    async def _rm():
        _, writer = await asyncio.open_unix_connection(DOCKER_SOCK)
        req = f"DELETE /containers/{cid}?force=true HTTP/1.1\r\nHost: localhost\r\n\r\n"
        writer.write(req.encode())
        await writer.drain()
        # Read response to avoid broken pipe
        await asyncio.sleep(0.1)
        writer.close()

    asyncio.run(_rm())


@pytest.fixture(scope="module")
def proxy_sock(tmp_path_factory):
    """Start a proxy for the test module, return socket path."""
    sock = str(tmp_path_factory.mktemp("proxy") / "test.sock")
    workspace = str(Path.cwd())

    pid = os.fork()
    if pid == 0:
        os.setsid()
        with contextlib.suppress(Exception):
            proxy_serve(sock, workspace)
        os._exit(1)

    for _ in range(50):
        if Path(sock).exists():
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGTERM)
        pytest.fail("Proxy did not start")

    # Pull alpine image through the proxy so tests can create containers
    asyncio.run(_send_request(sock, "POST", "/images/create?fromImage=alpine&tag=latest", b""))

    yield sock
    os.kill(pid, signal.SIGTERM)
    time.sleep(0.1)


async def _send_request(sock, method, path, body_bytes):  # noqa: C901
    """Low-level async HTTP request to a Unix socket."""
    reader, writer = await asyncio.open_unix_connection(sock)
    hdr = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n"
    if body_bytes:
        hdr += "Content-Type: application/json\r\n"
        hdr += f"Content-Length: {len(body_bytes)}\r\n"
    hdr += "Connection: close\r\n\r\n"
    writer.write(hdr.encode() + body_bytes)
    await writer.drain()

    status_line = await reader.readline()
    if not status_line:
        writer.close()
        return 0, b""
    status_code = int(status_line.split()[1])

    cl = 0
    chunked = False
    while True:
        line = await reader.readline()
        if line == b"\r\n" or not line:
            break
        low = line.lower()
        if low.startswith(b"content-length:"):
            cl = int(line.split(b":")[1].strip())
        if b"chunked" in low:
            chunked = True

    resp = b""
    if chunked:
        while True:
            size_line = await reader.readline()
            if not size_line:
                break
            size = int(size_line.strip(), 16)
            if size == 0:
                break
            resp += await reader.readexactly(size)
            await reader.readline()
    elif cl > 0:
        resp = await reader.read(cl)

    writer.close()
    return status_code, resp


def req(sock, method, path, body=None):
    """Send HTTP request to proxy, return (status, body_bytes)."""
    body_bytes = b""
    if body is not None:
        body_bytes = json.dumps(body).encode() if isinstance(body, dict) else body
    return asyncio.run(_send_request(sock, method, path, body_bytes))


class TestProxyIntegration:
    def test_ping(self, proxy_sock):
        status, _ = req(proxy_sock, "GET", "/_ping")
        assert status == 200

    def test_version(self, proxy_sock):
        status, body = req(proxy_sock, "GET", "/version")
        assert status == 200
        assert "ApiVersion" in json.loads(body)

    def test_container_list(self, proxy_sock):
        status, body = req(proxy_sock, "GET", "/containers/json")
        assert status == 200
        assert isinstance(json.loads(body), list)

    def test_image_list(self, proxy_sock):
        status, body = req(proxy_sock, "GET", "/images/json")
        assert status == 200
        assert isinstance(json.loads(body), list)

    def test_create_start_kill(self, proxy_sock):
        name = f"sux-pytest-{uuid.uuid4().hex[:8]}"
        status, body = req(
            proxy_sock,
            "POST",
            f"/containers/create?name={name}",
            {"Image": "alpine", "Cmd": ["sleep", "10"]},
        )
        assert status == 201, f"create failed: {body}"
        cid = json.loads(body)["Id"]

        try:
            status, _ = req(proxy_sock, "POST", f"/containers/{cid}/start")
            assert status in (204, 304)

            status, body = req(proxy_sock, "GET", f"/containers/{cid}/json")
            assert status == 200
            assert json.loads(body)["Id"] == cid

            status, _ = req(proxy_sock, "POST", f"/containers/{cid}/kill")
            assert status in (204, 409)
        finally:
            _force_remove(cid)

    def test_unowned_blocked(self, proxy_sock):
        status, _ = req(proxy_sock, "POST", "/containers/fake123/start")
        assert status == 403

    def test_delete_blocked(self, proxy_sock):
        status, _ = req(proxy_sock, "DELETE", "/containers/anything")
        assert status == 403

    def test_privileged_stripped(self, proxy_sock):
        status, body = req(
            proxy_sock,
            "POST",
            f"/containers/create?name=sux-pytest-{uuid.uuid4().hex[:8]}",
            {
                "Image": "alpine",
                "Cmd": ["true"],
                "HostConfig": {"Privileged": True},
            },
        )
        assert status == 201, f"create failed: {body}"
        cid = json.loads(body)["Id"]
        try:
            status, body = req(proxy_sock, "GET", f"/containers/{cid}/json")
            assert status == 200
            hc = json.loads(body)["HostConfig"]
            assert hc["Privileged"] is False
        finally:
            _force_remove(cid)

    def test_host_bind_stripped(self, proxy_sock):
        status, body = req(
            proxy_sock,
            "POST",
            f"/containers/create?name=sux-pytest-{uuid.uuid4().hex[:8]}",
            {
                "Image": "alpine",
                "Cmd": ["true"],
                "HostConfig": {"Binds": ["/etc:/mnt:ro"]},
            },
        )
        assert status == 201, f"create failed: {body}"
        cid = json.loads(body)["Id"]
        try:
            status, body = req(proxy_sock, "GET", f"/containers/{cid}/json")
            assert status == 200
            binds = json.loads(body)["HostConfig"].get("Binds") or []
            assert not any("/etc" in b for b in binds)
        finally:
            _force_remove(cid)

    def test_network_create_blocked(self, proxy_sock):
        status, _ = req(proxy_sock, "POST", "/networks/create", {"Name": "evil"})
        assert status == 403

    def test_volume_create_blocked(self, proxy_sock):
        status, _ = req(proxy_sock, "POST", "/volumes/create", {"Name": "evil"})
        assert status == 403
