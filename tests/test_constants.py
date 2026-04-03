from sux.constants import APT_PROFILES, SUX_DOCKERFILE, TMUX_CONFIG


def test_dockerfile_loads():
    assert len(SUX_DOCKERFILE) > 100
    assert "FROM debian:trixie" in SUX_DOCKERFILE


def test_tmux_config_loads():
    assert len(TMUX_CONFIG) > 50
    assert "set -g mouse on" in TMUX_CONFIG


def test_apt_profiles_structure():
    assert isinstance(APT_PROFILES, dict)
    for key, value in APT_PROFILES.items():
        assert isinstance(key, str)
        assert isinstance(value, list)
        for pkg in value:
            assert isinstance(pkg, str)


def test_apt_profiles_known():
    assert "gpu" in APT_PROFILES
    assert "go" in APT_PROFILES
    assert "llvm" in APT_PROFILES
