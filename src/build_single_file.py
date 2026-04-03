#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# ///
"""Collapse src/sux/ into a single executable Python file."""

import ast
import sys
from pathlib import Path

# Topological order matching the dependency DAG
MODULE_ORDER = [
    "constants",
    "utils",
    "git",
    "tmux",
    "proxy",
    "docker",
    "session",
    "config",
    "testing",
    "cli",
]

SHEBANG = "#!/usr/bin/env -S uv run --script"

UV_SCRIPT_META = """\
# /// script
# requires-python = ">=3.10"
# ///"""

SRC_DIR = Path("src/sux")

# Read the docstring from cli.py's MODULE_DOC
DOCSTRING = '''\
"""
sux - a tmux wrapper with sandboxing and screen keybindings

Modes:
  tmux      `sux <name>` creates or attaches to a named tmux session.
            `sux` with no args attaches to the most recent session.

  docker    `sux -d <name>` mounts the current directory into an isolated
            Docker container with rust, uv, node, and claude-code.
            Non-root user with sudo and NVIDIA GPU passthrough.
            `sux <name>` will automatically reattach if a container exists.

  worktree  `sux -w <name>` creates a git worktree in ./worktrees/<name>
            on a new branch and opens a tmux session in it.

  combined  `sux -w -d <name>` creates a worktree and runs it in Docker.

  yolo      `sux -w -y "prompt" <name>` creates a worktree, starts a
            Docker container, and runs claude --dangerously-skip-permissions.
            Requires -w for safety.

  kill      `sux -k <name>` kills the tmux session and removes the container.

  list      `sux -l` lists all tmux sessions and running Docker containers.

  config    `sux --config` writes a sane ~/.tmux.conf and rebuilds the
            Docker base image.
"""'''


def _collect_imports(source, tree):
    """Classify AST import nodes into stdlib imports and skip ranges."""
    stdlib_imports = []
    skip_ranges = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("sux"):
                skip_ranges.append((node.lineno, node.end_lineno))
            else:
                stdlib_imports.append(ast.get_source_segment(source, node))
                skip_ranges.append((node.lineno, node.end_lineno))
        elif isinstance(node, ast.Import):
            stdlib_imports.append(ast.get_source_segment(source, node))
            skip_ranges.append((node.lineno, node.end_lineno))

    return stdlib_imports, skip_ranges


def extract_module(filepath):
    """Extract stdlib imports and body from a module file.

    Returns (set of import lines, body text).
    Strips `from sux.*` imports and collects stdlib imports.
    """
    source = filepath.read_text()
    tree = ast.parse(source)

    stdlib_imports, skip_ranges = _collect_imports(source, tree)

    # Build set of lines to skip
    skip_lines = set()
    for start, end in skip_ranges:
        for ln in range(start, end + 1):
            skip_lines.add(ln)

    # Skip module docstring if present
    first_node = tree.body[0] if tree.body else None
    docstring_end = 0
    if (
        first_node
        and isinstance(first_node, ast.Expr)
        and isinstance(first_node.value, (ast.Constant, ast.Str))
    ):
        docstring_end = first_node.end_lineno

    # Reconstruct body without imports or docstring
    lines = source.splitlines()
    body_lines = []
    for lineno_0based, line in enumerate(lines):
        lineno = lineno_0based + 1
        if lineno in skip_lines:
            continue
        if lineno <= docstring_end:
            continue
        body_lines.append(line)

    body = "\n".join(body_lines).strip()
    return set(stdlib_imports), body


def build_constants_body():
    """Inline resource files as string literals for the single-file build."""
    resources = SRC_DIR / "resources"
    dockerfile = (resources / "Dockerfile").read_text()
    tmux_conf = (resources / "tmux.conf").read_text()

    # Read APT_PROFILES from constants.py source
    source = (SRC_DIR / "constants.py").read_text()
    tree = ast.parse(source)
    profiles_src = None
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "APT_PROFILES":
                    profiles_src = ast.get_source_segment(source, node)
    if not profiles_src:
        msg = "APT_PROFILES not found in constants.py"
        raise ValueError(msg)

    return (
        f"SUX_DOCKERFILE = {dockerfile!r}\n\n"
        f"TMUX_CONFIG = {tmux_conf!r}\n\n"
        f"{profiles_src}"
    )


def main():
    all_imports = set()
    bodies = []

    for mod_name in MODULE_ORDER:
        filepath = SRC_DIR / f"{mod_name}.py"
        if not filepath.exists():
            print(f"ERROR: {filepath} not found", file=sys.stderr)
            sys.exit(1)

        if mod_name == "constants":
            body = build_constants_body()
            bodies.append(f"\n# === {mod_name} ===\n\n{body}")
            continue

        imports, body = extract_module(filepath)
        all_imports |= imports

        if body:
            bodies.append(f"\n# === {mod_name} ===\n\n{body}")

    # Sort imports for consistency
    sorted_imports = sorted(all_imports)

    # Assemble the output
    parts = [
        SHEBANG,
        UV_SCRIPT_META,
        DOCSTRING,
        "",
        "\n".join(sorted_imports),
        "",
        "\n".join(bodies),
        "",
        "",
        'if __name__ == "__main__":',
        "    main()",
        "",
    ]

    output = "\n".join(parts)

    build_dir = Path("build")
    build_dir.mkdir(exist_ok=True)
    out_path = build_dir / "sux"
    out_path.write_text(output)
    out_path.chmod(0o755)
    print(f"Built: {out_path} ({len(output)} bytes)")


if __name__ == "__main__":
    main()
