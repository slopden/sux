FROM debian:trixie
RUN apt-get update && apt-get install -y \
    curl git build-essential pkg-config libssl-dev gnupg \
    ca-certificates sudo wget emacs nano libtbb-dev \
    jq htop ripgrep fd-find less openssh-client zip unzip tmux \
    && rm -rf /var/lib/apt/lists/*
# GPU_BLOCK_START
RUN wget -qO /tmp/cuda-keyring.deb \
      https://developer.download.nvidia.com/compute/cuda/repos/debian13/x86_64/cuda-keyring_1.1-1_all.deb && \
    dpkg -i /tmp/cuda-keyring.deb && rm /tmp/cuda-keyring.deb && \
    apt-get update && \
    apt-get install -y nsight-systems-2025.6.3 nsight-compute-2026.1.0 && \
    rm -rf /var/lib/apt/lists/*
# GPU_BLOCK_END
# APT_EXTRA
# Create non-root user matching host UID/GID/username
ARG UID=1000
ARG GID=1000
ARG USERNAME=user
RUN groupadd -g $GID $USERNAME && useradd -m -u $UID -g $GID -s /bin/bash $USERNAME
RUN echo "$USERNAME ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers
# Docker CLI only (daemon runs on host, accessed via proxy socket)
RUN install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg \
      -o /etc/apt/keyrings/docker.asc && \
    echo "deb [arch=$(dpkg --print-architecture) \
      signed-by=/etc/apt/keyrings/docker.asc] \
      https://download.docker.com/linux/debian trixie stable" \
      > /etc/apt/sources.list.d/docker.list && \
    apt-get update && \
    apt-get install -y docker-ce-cli && \
    rm -rf /var/lib/apt/lists/*
USER $USERNAME
ENV HOME=/home/$USERNAME
# Rust (latest stable via rustup)
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="$HOME/.cargo/bin:${PATH}"
# uv (latest)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="$HOME/.local/bin:${PATH}"
# Create the project venv outside the workspace bind mount; the host's .venv
# (if any) is masked by tmpfs, and a mount point can't be rmdir'd on recreate.
ENV UV_PROJECT_ENVIRONMENT=$HOME/.venv-sux
# nvm + latest Node.js
ENV NVM_DIR="$HOME/.nvm"
RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash \
    && . "$NVM_DIR/nvm.sh" && nvm install node
# Claude Code (native installer)
RUN curl -fsSL https://claude.ai/install.sh | bash
# Terminal colors and aliases
RUN cat >> $HOME/.bashrc <<'BASHRC'
export TERM=xterm-256color
PS1='\[\033[01;32m\]\u@\h\[\033[00m\]:\[\033[01;34m\]\W\[\033[00m\]\$ '
alias ls='ls --color=auto'
eval "$(dircolors -b)"
uv tool install ruff
alias pip='uv pip'
alias python='uv run python'
alias python3='uv run python'
alias yolo='claude --dangerously-skip-permissions'
BASHRC
# GPU_ENV_START
ENV NVIDIA_DRIVER_CAPABILITIES=all
# GPU_ENV_END
WORKDIR /workspace
