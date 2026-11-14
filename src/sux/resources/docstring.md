sux - a tmux wrapper with sandboxing and screen keybindings

Modes:
  tmux      `sux <name>` creates or attaches to a named tmux session.
            `sux` with no args attaches to the most recent session.

  docker    `sux -d <name>` mounts the current directory into an isolated
            Docker container with rust, uv, node, and claude-code.
            Non-root user with sudo and NVIDIA GPU passthrough.
            `sux <name>` will automatically reattach if a container exists.
            The repository's .git is read-only inside the container, so
            commit from the host. `--git-rw` lets the sandbox write git
            state (commits work, hooks and config stay read-only); this
            is best-effort, not a security boundary.

  worktree  `sux -w <name>` creates a git worktree for branch <name> in
            worktrees/<name> at the root of the checkout (for a submodule,
            the submodule's checkout; never inside .git), or reuses the
            worktree that branch is already checked out in, initialises
            its submodules and opens a tmux session in it.

  combined  `sux -w -d <name>` creates a worktree and runs it in Docker.

  yolo      `sux -y "cmd1; cmd2; ..." <name>` opens the session and types
            each `;`-delimited command into the pane via tmux send-keys,
            waiting for each to finish (sentinel-based) before sending the
            next. Combines with -d and/or -w.

  kill      `sux -k <name>` kills the tmux session and removes the container.

  list      `sux -l` lists all tmux sessions and running Docker containers.

  config    `sux --config` writes a sane ~/.tmux.conf and rebuilds the
            Docker base image.

  install   `sux install` lists the installer recipes and their state.
            `sux install <recipe>` checks this machine, prints the plan,
            then asks: apply / reprobe with sudo / exit.
