import json
from unittest.mock import patch

from sux.session import gpu_args


def docker_info(stdout):
    run = patch("sux.session.subprocess.run")
    mock_run = run.start()
    mock_run.return_value.stdout = stdout
    return run


class TestGpuArgs:
    def test_nvidia_runtime_present(self):
        runtimes = {"runc": {"path": "runc"}, "nvidia": {"path": "nvidia-runtime"}}
        run = docker_info(json.dumps(runtimes) + "\n")
        try:
            assert gpu_args() == ["--gpus", "all", "--runtime=nvidia"]
        finally:
            run.stop()

    def test_no_nvidia_runtime(self):
        run = docker_info(json.dumps({"runc": {"path": "runc"}}))
        try:
            assert gpu_args() == []
        finally:
            run.stop()

    def test_docker_info_failed(self):
        run = docker_info("")
        try:
            assert gpu_args() == []
        finally:
            run.stop()


class TestGitRwFlag:
    def run_cli(self, monkeypatch, *argv):
        from sux import cli  # noqa: PLC0415

        monkeypatch.setattr("sys.argv", ["sux", *argv])
        with patch("sux.cli.docker_session") as session:
            cli.main()
        return session

    def test_default_is_read_only(self, monkeypatch):
        session = self.run_cli(monkeypatch, "-d", "name")
        session.assert_called_once_with("name", commands=None, git_rw=False)

    def test_flag_reaches_docker_session(self, monkeypatch):
        session = self.run_cli(monkeypatch, "-d", "--git-rw", "name")
        session.assert_called_once_with("name", commands=None, git_rw=True)
