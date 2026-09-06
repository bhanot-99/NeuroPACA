#!/usr/bin/env bash
# Install the NeuroPACA daemon as a systemd --user service so it starts on
# every login and, with lingering enabled, survives logout and starts at boot.
#
#   scripts/install-user-service.sh              # install + enable + start
#   scripts/install-user-service.sh --no-linger  # skip `loginctl enable-linger`
#   scripts/install-user-service.sh --uninstall  # stop, disable, remove the unit
#
# It only ever touches ~/.config/systemd/user/ and your own systemd --user
# session — nothing system-wide, nothing that needs root.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$REPO/scripts/systemd/neuropacad.service"
UNIT_DST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/neuropacad.service"

die() { echo "error: $*" >&2; exit 1; }

command -v systemctl >/dev/null || die "systemctl not found — this needs systemd"
[ -f "$UNIT_SRC" ] || die "missing $UNIT_SRC"

if [ "${1:-}" = "--uninstall" ]; then
  systemctl --user disable --now neuropacad.service 2>/dev/null || true
  rm -f "$UNIT_DST"
  systemctl --user daemon-reload
  echo "removed $UNIT_DST"
  echo "(lingering, if you enabled it, is left as-is: loginctl disable-linger \"$USER\")"
  exit 0
fi

LINGER=1
[ "${1:-}" = "--no-linger" ] && LINGER=0

BIN="$REPO/.venv/bin/neuropacad"
[ -x "$BIN" ] || die "no $BIN — run: uv venv --python 3.12 && uv pip install -e \".[dev,llama,activity]\""

mkdir -p "$(dirname "$UNIT_DST")"
# The unit template carries __REPO__ placeholders (see its header comment).
sed "s|__REPO__|$REPO|g" "$UNIT_SRC" > "$UNIT_DST"
echo "wrote $UNIT_DST"

systemctl --user daemon-reload
systemctl --user enable --now neuropacad.service
echo "enabled + started neuropacad.service"

if [ "$LINGER" = 1 ]; then
  # Without lingering a --user unit stops at logout and only starts at the next
  # login. Lingering makes it start at boot and keep running across sessions.
  if loginctl enable-linger "$USER"; then
    echo "enabled lingering for $USER — the daemon now starts at boot"
  else
    echo "warning: could not enable lingering; the daemon will start at login only" >&2
  fi
fi

echo
systemctl --user --no-pager status neuropacad.service | head -n 12 || true
echo
echo "check it:   neuropaca health        (or: neuropaca  ->  \$health)"
echo "logs:       journalctl --user -u neuropacad -f"
echo "undo:       scripts/install-user-service.sh --uninstall"
