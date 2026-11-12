import json
from unittest.mock import patch

from sux.proxy import (
    _owned_containers,
    check_request,
    ensure_proxy,
    proxy_paths,
    sanitize_binds,
    sanitize_create,
    sanitize_exec,
    start_proxy,
)


class TestSanitizeBinds:
    def test_workspace_bind_passes(self):
        result = sanitize_binds(["/workspace/src:/app"], "/home/user/project")
        assert result == ["/home/user/project/src:/app"]

    def test_workspace_root_bind_passes(self):
        result = sanitize_binds(["/workspace:/app"], "/home/user/project")
        assert result == ["/home/user/project:/app"]

    def test_non_workspace_bind_blocked(self):
        result = sanitize_binds(["/etc:/mnt:ro"], "/home/user/project")
        assert result == []

    def test_relative_bind_passes(self):
        result = sanitize_binds(["myvolume:/data"], "/home/user/project")
        assert result == ["myvolume:/data"]

    def test_empty_binds(self):
        assert sanitize_binds([], "/workspace") == []
        assert sanitize_binds(None, "/workspace") == []

    def test_mixed_binds(self):
        binds = [
            "/workspace/data:/data",
            "/etc/passwd:/etc/passwd:ro",
            "named-vol:/vol",
        ]
        result = sanitize_binds(binds, "/home/user/proj")
        assert len(result) == 2
        assert result[0] == "/home/user/proj/data:/data"
        assert result[1] == "named-vol:/vol"

    def test_path_traversal_blocked(self):
        result = sanitize_binds(["/workspace/../etc/shadow:/mnt"], "/home/user")
        assert result == []

    def test_path_traversal_deep_blocked(self):
        result = sanitize_binds(["/workspace/foo/../../etc:/mnt"], "/home/user")
        assert result == []

    def test_normalized_workspace_passes(self):
        result = sanitize_binds(["/workspace/./src:/app"], "/home/user/proj")
        assert result == ["/home/user/proj/src:/app"]

    def test_host_path_workspace_passes(self):
        # worktrees are mounted at their host path inside the container
        ws = "/home/user/repo/worktrees/x"
        result = sanitize_binds([f"{ws}/src:/app:ro", f"{ws}:/all"], ws)
        assert result == [f"{ws}/src:/app:ro", f"{ws}:/all"]

    def test_host_path_traversal_blocked(self):
        ws = "/home/user/repo/worktrees/x"
        binds = [f"{ws}/../../.git:/mnt", f"{ws}-other:/mnt", "/home/user:/mnt"]
        assert sanitize_binds(binds, ws) == []


class TestProxyWorkspace:
    def test_start_saves_workspace(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        sock, pidfile, _, wsfile = proxy_paths("t")
        (tmp_path / "sux-proxy-t.sock").touch()

        with patch("sux.proxy.os.fork", return_value=4242):
            assert start_proxy("t") == sock

        assert (tmp_path / "sux-proxy-t.pid").read_text() == "4242"
        assert pidfile == str(tmp_path / "sux-proxy-t.pid")
        assert (tmp_path / "sux-proxy-t.workspace").read_text() == str(tmp_path)
        assert wsfile == str(tmp_path / "sux-proxy-t.workspace")

    def test_restart_uses_saved_workspace_not_cwd(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        (tmp_path / "sux-proxy-t.pid").write_text("not a pid")
        (tmp_path / "sux-proxy-t.workspace").write_text("/home/user/repo")

        with patch("sux.proxy.start_proxy") as start:
            ensure_proxy("t")

        start.assert_called_once_with("t", "/home/user/repo")

    def test_first_start_has_no_saved_workspace(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        with patch("sux.proxy.start_proxy") as start:
            ensure_proxy("t")
        start.assert_called_once_with("t", None)


class TestSanitizeCreate:
    def test_strips_privileged(self):
        body = json.dumps(
            {
                "Image": "alpine",
                "HostConfig": {"Privileged": True, "Binds": []},
            }
        )
        result = json.loads(sanitize_create(body, "/workspace"))
        assert "Privileged" not in result.get("HostConfig", {})

    def test_rewrites_host_network(self):
        body = json.dumps(
            {
                "Image": "alpine",
                "HostConfig": {"NetworkMode": "host"},
            }
        )
        result = json.loads(sanitize_create(body, "/workspace"))
        assert result["HostConfig"]["NetworkMode"] == "bridge"

    def test_strips_non_workspace_binds(self):
        body = json.dumps(
            {
                "Image": "alpine",
                "HostConfig": {"Binds": ["/etc:/mnt:ro", "/workspace/src:/app"]},
            }
        )
        result = json.loads(sanitize_create(body, "/home/user/proj"))
        assert result["HostConfig"]["Binds"] == ["/home/user/proj/src:/app"]

    def test_preserves_allowed_fields(self):
        body = json.dumps(
            {
                "Image": "alpine",
                "Cmd": ["echo", "hi"],
                "Env": ["FOO=bar"],
                "WorkingDir": "/app",
            }
        )
        result = json.loads(sanitize_create(body, "/workspace"))
        assert result["Image"] == "alpine"
        assert result["Cmd"] == ["echo", "hi"]
        assert result["Env"] == ["FOO=bar"]

    def test_strips_unknown_top_level(self):
        body = json.dumps(
            {
                "Image": "alpine",
                "Domainname": "evil.com",
                "CapAdd": ["SYS_ADMIN"],
            }
        )
        result = json.loads(sanitize_create(body, "/workspace"))
        assert "Domainname" not in result
        assert "CapAdd" not in result
        assert result["Image"] == "alpine"

    def test_invalid_json_returns_body(self):
        result = sanitize_create(b"not json", "/workspace")
        assert result == b"not json"

    def test_strips_port_bindings(self):
        body = json.dumps(
            {
                "Image": "alpine",
                "HostConfig": {"PortBindings": {"80/tcp": [{"HostPort": "8080"}]}},
            }
        )
        result = json.loads(sanitize_create(body, "/workspace"))
        assert "PortBindings" not in result.get("HostConfig", {})

    def test_preserves_allowed_hostconfig(self):
        body = json.dumps(
            {
                "Image": "alpine",
                "HostConfig": {
                    "Memory": 1073741824,
                    "NanoCpus": 2000000000,
                    "Runtime": "nvidia",
                    "DeviceRequests": [{"Count": -1}],
                },
            }
        )
        result = json.loads(sanitize_create(body, "/workspace"))
        hc = result["HostConfig"]
        assert hc["Memory"] == 1073741824
        assert hc["Runtime"] == "nvidia"


class TestSanitizeExec:
    def test_strips_privileged(self):
        body = json.dumps({"Cmd": ["bash"], "Privileged": True, "User": "root"})
        result = json.loads(sanitize_exec(body))
        assert "Privileged" not in result
        assert "User" not in result
        assert result["Cmd"] == ["bash"]

    def test_preserves_allowed(self):
        body = json.dumps(
            {
                "AttachStdin": True,
                "AttachStdout": True,
                "Tty": True,
                "Cmd": ["sh"],
            }
        )
        result = json.loads(sanitize_exec(body))
        assert result["AttachStdin"] is True
        assert result["Cmd"] == ["sh"]

    def test_invalid_json(self):
        assert sanitize_exec(b"nope") == b"nope"


class TestCheckRequest:
    def setup_method(self):
        _owned_containers.clear()

    def test_ping(self):
        ok, _ = check_request("GET", "/_ping", b"", "/ws")
        assert ok
        ok, _ = check_request("HEAD", "/_ping", b"", "/ws")
        assert ok

    def test_version(self):
        ok, _ = check_request("GET", "/version", b"", "/ws")
        assert ok

    def test_info(self):
        ok, _ = check_request("GET", "/info", b"", "/ws")
        assert ok

    def test_container_list(self):
        ok, _ = check_request("GET", "/containers/json", b"", "/ws")
        assert ok

    def test_images_list(self):
        ok, _ = check_request("GET", "/images/json", b"", "/ws")
        assert ok

    def test_image_inspect(self):
        ok, _ = check_request("GET", "/v1.41/images/abc123/json", b"", "/ws")
        assert ok

    def test_pull(self):
        ok, _ = check_request("POST", "/images/create?fromImage=alpine", b"", "/ws")
        assert ok

    def test_build(self):
        ok, _ = check_request("POST", "/build", b"", "/ws")
        assert ok

    def test_container_create(self):
        body = json.dumps({"Image": "alpine"}).encode()
        ok, rewritten = check_request("POST", "/containers/create", body, "/ws")
        assert ok
        assert b"alpine" in rewritten

    def test_owned_container_start(self):
        _owned_containers.add("abc123full")
        ok, _ = check_request("POST", "/containers/abc123full/start", b"", "/ws")
        assert ok

    def test_owned_container_stop(self):
        _owned_containers.add("abc123full")
        ok, _ = check_request("POST", "/containers/abc123full/stop", b"", "/ws")
        assert ok

    def test_owned_container_kill(self):
        _owned_containers.add("abc123full")
        ok, _ = check_request("POST", "/containers/abc123full/kill", b"", "/ws")
        assert ok

    def test_owned_container_inspect(self):
        _owned_containers.add("abc123full")
        ok, _ = check_request("GET", "/containers/abc123full/json", b"", "/ws")
        assert ok

    def test_owned_container_exec_sanitized(self):
        _owned_containers.add("abc123full")
        body = json.dumps({"Cmd": ["bash"], "Privileged": True}).encode()
        ok, rewritten = check_request(
            "POST", "/containers/abc123full/exec", body, "/ws"
        )
        assert ok
        assert b"Privileged" not in rewritten

    def test_unowned_container_blocked(self):
        ok, _ = check_request("POST", "/containers/other123/start", b"", "/ws")
        assert not ok

    def test_unowned_container_inspect_blocked(self):
        ok, _ = check_request("GET", "/containers/other123/json", b"", "/ws")
        assert not ok

    def test_delete_blocked(self):
        _owned_containers.add("abc123full")
        ok, _ = check_request("DELETE", "/containers/abc123full", b"", "/ws")
        assert not ok

    def test_export_blocked(self):
        _owned_containers.add("abc123full")
        ok, _ = check_request("GET", "/containers/abc123full/export", b"", "/ws")
        assert not ok

    def test_logs_blocked(self):
        _owned_containers.add("abc123full")
        ok, _ = check_request("GET", "/containers/abc123full/logs", b"", "/ws")
        assert not ok

    def test_volumes_blocked(self):
        ok, _ = check_request("POST", "/volumes/create", b"{}", "/ws")
        assert not ok

    def test_networks_blocked(self):
        ok, _ = check_request("POST", "/networks/create", b"{}", "/ws")
        assert not ok

    def test_exec_start_allowed(self):
        ok, _ = check_request("POST", "/exec/execid123/start", b"{}", "/ws")
        assert ok

    def test_exec_resize_allowed(self):
        ok, _ = check_request("POST", "/exec/execid123/resize", b"", "/ws")
        assert ok

    def test_versioned_paths(self):
        ok, _ = check_request("GET", "/v1.41/_ping", b"", "/ws")
        assert ok
        ok, _ = check_request("GET", "/v1.41/containers/json", b"", "/ws")
        assert ok

    def test_prefix_matching(self):
        _owned_containers.add("abc123fullhash")
        ok, _ = check_request("POST", "/containers/abc123fullhash/start", b"", "/ws")
        assert ok
        # Short prefix also matches
        ok, _ = check_request("POST", "/containers/abc123/start", b"", "/ws")
        assert ok
