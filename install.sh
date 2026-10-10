#!/bin/sh
set -eu
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
command -v python3 >/dev/null
command -v tmux >/dev/null
command -v systemctl >/dev/null
mkdir -p "$HOME/.local/bin" "$HOME/.codex/skills"
for command_name in codex-supervisor codex-supervisor-bind; do
  target="$HOME/.local/bin/$command_name"
  if [ -e "$target" ] || [ -L "$target" ]; then
    if [ "$(readlink -f "$target")" != "$repo_root/bin/$command_name" ]; then
      echo "Refusing to overwrite existing $target" >&2
      exit 1
    fi
  else
    ln -s "$repo_root/bin/$command_name" "$target"
  fi
done
skill_target="$HOME/.codex/skills/codex-thread-supervisor"
if [ -e "$skill_target" ] || [ -L "$skill_target" ]; then
  if [ "$(readlink -f "$skill_target")" != "$repo_root/skills/codex-thread-supervisor" ]; then
    echo "Commands installed; existing supervisor skill preserved: $skill_target" >&2
  fi
else
  ln -s "$repo_root/skills/codex-thread-supervisor" "$skill_target"
fi
echo 'Installed. Keep this checkout; add ~/.local/bin to PATH if needed.'
echo 'No conversation bound, timer started, or running supervisor changed.'
