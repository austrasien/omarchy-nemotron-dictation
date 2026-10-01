#!/usr/bin/env bash
# Install the Nemotron live-dictation sidecar on Omarchy.
# Does not write /usr/share/omarchy or ~/.config/uwsm/env.d/.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
MARKER=omarchy-nemotron-dictation
BIN_DST="${HOME}/.config/omarchy/bin"
UNIT_DST="${HOME}/.config/systemd/user"
PHRASES_DST="${HOME}/.config/omarchy/nemotron/phrases.txt"
LOCAL_BIN="${HOME}/.local/bin"
NEMO_INSTALL_SH="https://github.com/NVIDIA/NeMo-Speech.cpp/raw/main/scripts/install.sh"
HUD=1
PULL=1
BINDS=1

usage() {
  cat <<EOF
usage: ./install.sh [--no-hud] [--no-pull] [--no-binds]

  --no-hud     skip \`omarchy plugin add\` for Omakoe
  --no-pull    skip downloading the Nemotron 3.5 GGUF (~700 MB)
  --no-binds   skip writing Hyprland Super+Ctrl+X / F9 / wtype keybinds
EOF
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --no-hud) HUD=0 ;;
    --no-pull) PULL=0 ;;
    --no-binds) BINDS=0 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing dependency: $1" >&2
    exit 1
  }
}

need curl
need tar
need python3
need systemctl
need wpctl
need pw-record
need wtype

mkdir -p "$BIN_DST" "$UNIT_DST" "$(dirname "$PHRASES_DST")" "$LOCAL_BIN"

install -m 0755 "$ROOT/bin/nemotron-dictation" "$BIN_DST/nemotron-dictation"
install -m 0755 "$ROOT/bin/nemotron-dictation-client.py" "$BIN_DST/nemotron-dictation-client.py"
install -m 0755 "$ROOT/bin/voxtype-audio-bridge" "$BIN_DST/voxtype-audio-bridge"
ln -sfn "$BIN_DST/nemotron-dictation" "$LOCAL_BIN/nemotron-dictation"
ln -sfn "$BIN_DST/voxtype-audio-bridge" "$LOCAL_BIN/voxtype-audio-bridge"

install -m 0644 "$ROOT/systemd/nemo-speech.service" "$UNIT_DST/nemo-speech.service"
install -m 0644 "$ROOT/systemd/nemotron-dictation-client.service" "$UNIT_DST/nemotron-dictation-client.service"

if [[ ! -f $PHRASES_DST ]]; then
  install -m 0644 "$ROOT/phrases.example.txt" "$PHRASES_DST"
  echo "created $PHRASES_DST (example). Edit it with your own names — see README."
else
  echo "kept existing $PHRASES_DST"
fi

append_block() {
  local dest=$1 snippet=$2
  [[ -f $dest ]] || {
    echo "skip Hyprland snippet: $dest is missing" >&2
    return 0
  }
  if grep -q "BEGIN ${MARKER}" "$dest"; then
    echo "already patched $dest"
    return 0
  fi
  printf '\n' >>"$dest"
  cat "$snippet" >>"$dest"
  echo "patched $dest"
}

if [[ $BINDS -eq 1 ]]; then
  append_block "${HOME}/.config/hypr/bindings.lua" "$ROOT/hypr/bindings.lua.snippet"
  append_block "${HOME}/.config/hypr/input.lua" "$ROOT/hypr/input.lua.snippet"
fi

if [[ ! -x ${HOME}/.local/bin/nemo-speech ]]; then
  echo "installing nemo-speech (CPU binary) into ~/.local …"
  curl -fsSL "$NEMO_INSTALL_SH" | sh -s -- \
    --version 0.1.0 \
    --backend cpu \
    --binary-only \
    --no-modify-path \
    --prefix "$HOME/.local"
fi
command -v nemo-speech >/dev/null 2>&1 || export PATH="$HOME/.local/bin:$PATH"
command -v nemo-speech >/dev/null 2>&1 || {
  echo "nemo-speech is not on PATH. Add ~/.local/bin then re-run." >&2
  exit 1
}

if [[ $PULL -eq 1 ]]; then
  echo "pulling nemotron-3.5 (first time ~700 MB) …"
  nemo-speech pull nemotron-3.5
fi

systemctl --user daemon-reload
systemctl --user enable --now nemo-speech.service

if command -v voxtype >/dev/null 2>&1; then
  voxtype config set osd.enabled false 2>/dev/null || true
  echo "Voxtype stays installed for rollback. Super+Ctrl+X now toggles Nemotron;"
  echo "the toggle stops voxtype.service for the duration of a take."
fi

if [[ $HUD -eq 1 ]] && command -v omarchy >/dev/null 2>&1; then
  omarchy plugin add https://github.com/ok/omakoe.git --enable || {
    echo "Omakoe plugin add failed. Install later with:" >&2
    echo "  omarchy plugin add https://github.com/ok/omakoe.git --enable" >&2
  }
  echo "restart the shell so Omakoe picks up voxtype-audio-bridge from PATH:"
  echo "  omarchy restart shell"
fi

if command -v hyprctl >/dev/null 2>&1; then
  hyprctl reload >/dev/null 2>&1 || true
  hyprctl eval 'hl.device({ name = "hl-virtual-keyboard-wtype", keybinds = false })' >/dev/null 2>&1 || true
  hyprctl eval 'hl.device({ name = "hl-virtual-keyboard-fcitx5", keybinds = false })' >/dev/null 2>&1 || true
fi

echo
echo "done. Super+Ctrl+X or F9 toggles live French dictation."
echo "Escape cancels (drops leftover words). Toggle again to stop and flush."
echo "Personal names: edit $PHRASES_DST then start a new take."
