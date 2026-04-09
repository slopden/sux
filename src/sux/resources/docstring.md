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
