#!/usr/bin/env bash
# One unified script for the whole voice-standalone system: the daemon
# (always-on voice command listener), the tray icon (manual trigger +
# visibility), and the 7-day soak monitor — installed and enabled
# together, as one coordinated set, not three separate manual steps.
#
# All three run as systemd --user services tied to graphical-session.target:
# that's what makes "starts with turning on the system, closes safely on
# shutdown" actually true. graphical-session.target starts once you log
# into the desktop session (compositor/D-Bus/audio are up by then — these
# services need real audio devices and a notification server, not just
# the OS booting), and systemd sends each one a normal SIGTERM when that
# session ends, whether that's a logout or a real shutdown — no separate
# shutdown hook needed, this is systemd's own session lifecycle, not
# something built here.
#
# Idempotent: re-running this after a code change just re-installs the
# unit files and restarts the daemon/tray (soak monitor is intentionally
# NOT restarted by a re-run — see below) — safe to run again any time.
#
# Usage:
#   ./setup_services.sh              # install + enable + start all three
#   ./setup_services.sh --uninstall  # stop + disable + remove all three

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
SERVICES=(voice-daemon voice-tray voice-soak)

if [[ "${1:-}" == "--uninstall" ]]; then
    echo "Stopping and disabling voice-standalone services..."
    for svc in "${SERVICES[@]}"; do
        systemctl --user disable --now "$svc" 2>/dev/null || true
        rm -f "$UNIT_DIR/${svc}.service"
    done
    systemctl --user daemon-reload
    echo "Done. (soak_log.jsonl / soak_summary.json / audit.jsonl are left alone — those are your data, not install artifacts.)"
    exit 0
fi

if [[ ! -f "$REPO_DIR/.venv/bin/python" ]]; then
    echo "ERROR: $REPO_DIR/.venv not found — set up the venv first (see ARCHITECTURE.md)." >&2
    exit 1
fi
if [[ ! -f "$REPO_DIR/piper_voices/en_IN-spicor.onnx" ]]; then
    echo "ERROR: Piper voice model not found — see ARCHITECTURE.md's Step 7 setup section to download it." >&2
    exit 1
fi

mkdir -p "$UNIT_DIR"

echo "Installing unit files into $UNIT_DIR ..."
for svc in "${SERVICES[@]}"; do
    sed "s|__REPO__|$REPO_DIR|g" "$REPO_DIR/systemd/${svc}.service" > "$UNIT_DIR/${svc}.service"
done

systemctl --user daemon-reload

echo "Enabling voice-daemon and voice-tray (always-on, restart on every boot)..."
systemctl --user enable --now voice-daemon voice-tray

# voice-soak is deliberately NOT included in the enable/restart above:
# it's a bounded 7-day run, not an always-on service — `enable` alone
# (no --now, and no restart on a re-run of this script) means it starts
# fresh on the NEXT real login/boot, but re-running this script to pick
# up a code change to daemon.py doesn't quietly reset an in-progress
# soak back to day 0. Start it explicitly the first time with:
#   systemctl --user enable --now voice-soak
if systemctl --user is-active --quiet voice-soak; then
    echo "voice-soak already running — left alone (re-run this script doesn't reset an in-progress soak)."
else
    systemctl --user enable voice-soak
    echo "voice-soak enabled (starts on next login/boot), not started now."
    echo "To start the 7-day soak immediately instead of waiting for next boot:"
    echo "  systemctl --user start voice-soak"
fi

echo
echo "Status:"
# `|| true`: systemctl status exits non-zero for the (expected,
# by-design) inactive voice-soak — that's informational output, not a
# reason for this script itself to report failure via set -e.
systemctl --user status voice-daemon voice-tray voice-soak --no-pager -l 2>&1 | grep -E "●|Active:" || true
