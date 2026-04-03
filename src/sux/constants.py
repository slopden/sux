from pathlib import Path

_RESOURCES = Path(__file__).parent / "resources"

SUX_DOCKERFILE = (_RESOURCES / "Dockerfile").read_text()
TMUX_CONFIG = (_RESOURCES / "tmux.conf").read_text()

APT_PROFILES = {
    "gpu": [
        "nsight-systems-2025.6.3",
        "nsight-compute-2026.1.0",
        "vulkan-tools",
        "weston",
    ],
    "go": ["golang"],
    "llvm": ["clang", "lldb", "lld", "clang-format", "clang-tidy"],
}
