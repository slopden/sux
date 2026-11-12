<p align="center">
  <img src="static/logo.svg" width="128" height="128" alt="sux logo">
</p>

# sux

A tmux wrapper with sandboxing and screen keybindings that mounts your Claude settings into a container so you can run in pretty-much-yolo-mode. Probably not perfect, but still much better than no sandbox. You can also run "just tmux" sessions with no sandbox, but I wouldn't run yolo-mode without aa sandbox.

## Linux Install

You'll have to install `tmux` and `docker` yourself to get anything out of this.

Then, if you have `uv` installed you can use the single-file script version of `sux` which is easy to edit locally and much smaller:
```bash
curl -Lo ~/.local/bin/sux https://github.com/slopden/sux/releases/latest/download/sux && chmod +x ~/.local/bin/sux
```

From a checkout, `make install` builds the single-file script and writes it to `~/.local/bin/sux`.

`sux` is also packaged as a self-contained executable using [nuitka](https://nuitka.net/) which has no runtime dependencies:
```bash
curl -Lo ~/.local/bin/sux https://github.com/slopden/sux/releases/latest/download/sux-linux-x64 && chmod +x ~/.local/bin/sux
```



## Workflow

Set up docker and Claude in your main system. Then you can run on a git repo:

```bash
# one-time setup will write a tmux config and build a docker image
sux --config --apt=gpu

# move into a git repo you've already cloned
# this will create a branch/worktree and open it in a docker sandbox
sux -w -d feature

# once in the docker sandbox run Claude with permissions for everything
yolo 

# after it's doing something, detach from the session: `Ctrl-a Ctrl-d`


# back on your machine, list all sessions
sux -l

# reattach to the tmux/docker session
sux feature

# detach, re-attach, etc. When you're done in the worktree do a commit
# and on the parent you can then do a:
git merge feature

# clean up the worktree you no longer need
rm -rf worktrees/feature && git worktree prune

# kill the session and delete the docker image
sux -k feature
```


## Git in the sandbox

By default the repository's `.git` is mounted read-only in the container: the sandbox can read history and edit files, and you commit from the host. `sux -d --git-rw <name>` mounts `.git` writable so commits work inside the container, with every `hooks` directory and `config` file mounted read-only on top. That mode is best-effort, not a security boundary: a determined process can still get around the hook protection, for example by renaming a submodule or worktree git dir and creating its own in its place. In either mode the `.git` pointer files of submodules inside the workspace are writable (only the workspace root's own pointer is read-only), so look before running git on the host in a directory a sandbox has worked in. The mode is fixed when the container is created; `sux -k <name>` and start again to change it.

## Developing Sux

```bash
make single       # build/sux (single-file script)
make executable   # build/sux-bin (nuitka binary)
make test         # lint + 72 tests
```
