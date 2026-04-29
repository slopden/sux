from sux.docker import prepare_dockerfile, resolve_apt_extras


class TestResolveAptExtras:
    def test_profile_expansion(self):
        result = resolve_apt_extras(["go"])
        assert "golang" in result

    def test_literal_passthrough(self):
        result = resolve_apt_extras(["vim"])
        assert result == ["vim"]

    def test_mixed_profiles_and_literals(self):
        result = resolve_apt_extras(["go", "vim", "llvm"])
        assert "golang" in result
        assert "vim" in result
        assert "clang" in result

    def test_deduplication(self):
        result = resolve_apt_extras(["go", "golang"])
        assert result.count("golang") == 1

    def test_sorted(self):
        result = resolve_apt_extras(["zsh", "ack", "go"])
        assert result == sorted(result)


class TestPrepareDockerfile:
    def test_default_is_kitchen_sink(self):
        df = prepare_dockerfile()
        assert "# APT_EXTRA" not in df
        assert "# GPU_BLOCK_START" not in df
        assert "# GPU_BLOCK_END" not in df
        assert "nsight" in df
        assert "NVIDIA_DRIVER_CAPABILITIES" in df
        assert "golang" in df
        assert "clang" in df

    def test_minimal_is_explicit_empty_list(self):
        df = prepare_dockerfile(apt_extras=[])
        assert "# APT_EXTRA" not in df
        assert "# GPU_BLOCK_START" not in df
        assert "# GPU_BLOCK_END" not in df
        assert "nsight" not in df
        assert "NVIDIA_DRIVER" not in df

    def test_gpu_profile_keeps_gpu_block(self):
        df = prepare_dockerfile(apt_extras=["gpu"])
        assert "nsight-systems" in df
        assert "NVIDIA_DRIVER_CAPABILITIES" in df
        assert "# GPU_BLOCK_START" not in df
        assert "# GPU_BLOCK_END" not in df

    def test_apt_extras_injected(self):
        df = prepare_dockerfile(apt_extras=["vim", "zsh"])
        assert "apt-get install -y vim zsh" in df

    def test_gpu_plus_extras(self):
        df = prepare_dockerfile(apt_extras=["gpu", "golang"])
        assert "nsight-systems" in df
        assert "golang" in df
