#!/usr/bin/env bash
# Remove the sidecar. Does not delete a personal phrases.txt unless --purge.
# Does not uninstall the nemo-speech binary unless --purge.
set -euo pipefail

MARKER=omarchy-nemotron-dictation
BIN_DST="${HOME}/.config/omarchy/bin"
UNIT_DST="${HOME}/.config/systemd/user"
LOCAL_BIN="${HOME}/.local/bin"
PURGE=0

if [[ ${1:-} == --purge ]]; then
  PURGE=1
elif [[ $# -gt 0 ]]; then
  echo "usage: ./uninstall.sh [--purge]" >&2
  exit 2
fi

systemctl --user stop nemotron-dictation-client.service 2>/dev/null || true
systemctl --user disable --now nemo-speech.service 2>/dev/null || true

rm -f "$UNIT_DST/nemo-speech.service" \
  "$UNIT_DST/nemotron-dictation-client.service" \
  "$BIN_DST/nemotron-dictation" \
  "$BIN_DST/nemotron-dictation-client.py" \
  "$BIN_DST/voxtype-audio-bridge" \
  "$LOCAL_BIN/nemotron-dictation" \
  "$LOCAL_BIN/voxtype-audio-bridge"

systemctl --user daemon-reload

strip_block() {
  local dest=$1
  [[ -f $dest ]] || return 0
  grep -q "BEGIN ${MARKER}" "$dest" || return 0
  python3 - "$dest" "$MARKER" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
marker = sys.argv[2]
text = path.read_text(encoding="utf-8")
begin = f"-- BEGIN {marker}"
end = f"-- END {marker}"
out, skip, i = [], False, 0
for line in text.splitlines(keepends=True):
    if line.startswith(begin):
        skip = True
        continue
    if skip and line.startswith(end):
        skip = False
        continue
    if not skip:
        out.append(line)
path.write_text("".join(out), encoding="utf-8")
PY
  echo "restored $dest"
}

strip_block "${HOME}/.config/hypr/bindings.lua"
strip_block "${HOME}/.config/hypr/input.lua"
command -v hyprctl >/dev/null 2>&1 && hyprctl reload >/dev/null 2>&1 || true

if [[ $PURGE -eq 1 ]]; then
  rm -f "${HOME}/.config/omarchy/nemotron/phrases.txt"
  echo "removed phrases.txt"
  echo "nemo-speech binary and GGUF left in place (hundreds of MB)."
  echo "remove them yourself if you want:"
  echo "  rm -f ~/.local/bin/nemo-speech"
  echo "  rm -rf ~/.local/share/nemo-speech ~/.cache/nemo-speech"
else
  echo "kept ~/.config/omarchy/nemotron/phrases.txt"
fi

echo "uninstalled. To return to Voxtype: systemctl --user enable --now voxtype"
