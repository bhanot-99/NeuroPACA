#!/usr/bin/env bash
# NeuroPACA — bring the whole live stack up in one step: the daemon, the
# 7-day soak supervisor, and the presence tray. One script instead of
# several separate `systemctl` dances, and idempotent — safe to re-run any
# time to pick up a config or code change.
#
# WHY A RESTART, NOT JUST "IS IT RUNNING"
#
# neuropacad.service is very likely already active (it's `enabled` and
# `WantedBy=graphical-session.target`) — but config is only read at process
# start, so if neuropaca.toml changed since the daemon last started a plain
# "start if not running" would
# silently keep the OLD config resident. This script always restarts
# neuropacad so what's running matches what's on disk, and relies on the
# 7-day soak's own healed-time accounting (scripts/soak_state.py) to treat
# that restart as just another session boundary, the same as any crash or
# reboot it already has to tolerate.
#
# USAGE
#   scripts/neuropaca_live.sh            # (re)start everything, then show status
#   scripts/neuropaca_live.sh --status   # just print status, touch nothing
#
# EXIT
#   0  everything is up (or, for --status, everything queried cleanly)
#   1  a preflight check failed, or a unit did not come up — never both
#      silently succeeding AND being broken (rules.md §2's "never fails
#      silently" applied to a script instead of a handler)

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

UNIT_DIR="${HOME}/.config/systemd/user"
# Order matters only for readability here — none of these Requires= each
# other except neuropaca-soak.service -> neuropacad.service, which the unit
# files themselves already declare (After=/Requires=), not this script.
UNITS=(neuropacad neuropaca-tray neuropaca-soak neuropaca-soak-tray)

# Resolve which config file neuropacad.service ACTUALLY runs under. Found
# the hard way (2026-09-14): a machine-local drop-in
# (${UNIT_DIR}/neuropacad.service.d/override.conf) redirects NEUROPACA_CONFIG
# away from neuropaca.toml on this box. Reading the drop-in here instead of
# hardcoding neuropaca.toml is what let a first version of this script
# "successfully" validate and restart the daemon while checking a file the
# daemon doesn't actually read.
LIVE_CONFIG="neuropaca.toml"
override_conf="${UNIT_DIR}/neuropacad.service.d/override.conf"
if [ -f "$override_conf" ]; then
    from_override="$(grep -oP '(?<=^Environment=NEUROPACA_CONFIG=).+' "$override_conf" | tail -1 || true)"
    if [ -n "$from_override" ]; then
        LIVE_CONFIG="$from_override"
        echo "-- neuropacad.service is redirected by a systemd drop-in to: ${LIVE_CONFIG}"
    fi
fi

status_only=0
force_units=0
for arg in "$@"; do
    case "$arg" in
        --status) status_only=1 ;;
        --refresh-units) force_units=1 ;;
    esac
done

print_status() {
    echo
    echo "=== unit status ==="
    for u in "${UNITS[@]}"; do
        state="$(systemctl --user is-active "${u}.service" 2>/dev/null || true)"
        enabled="$(systemctl --user is-enabled "${u}.service" 2>/dev/null || true)"
        printf '  %-28s %-10s (%s)\n' "${u}.service" "${state:-unknown}" "${enabled:-unknown}"
    done

    echo
    echo "  live config: ${LIVE_CONFIG}"
    if [ ! -f data/health.json ]; then
        echo "  data/health.json not found yet — daemon may still be starting"
    fi

    echo
    echo "=== 7-day soak ==="
    ./scripts/soak_7day.sh --status 2>&1 | sed 's/^/  /' || echo "  (soak status unavailable)"

    echo
    echo "logs:    tail -f data/neuropaca.log"
    echo "actions: tail -f data/actions.jsonl"
}

if [ "$status_only" -eq 1 ]; then
    print_status
    exit 0
fi

echo "=== NeuroPACA — bringing the live stack up ==="

# ---------------------------------------------------------------- preflight
[ -x .venv/bin/neuropacad ] || {
    echo "HALT — .venv/bin/neuropacad not found. Run: uv pip install -e '.'"
    exit 1
}
[ -f "$LIVE_CONFIG" ] || { echo "HALT — ${LIVE_CONFIG} is missing."; exit 1; }

echo "-- validating ${LIVE_CONFIG}"
.venv/bin/python3 -c "
import tomllib
from neuropaca.core.config import Config
with open('${LIVE_CONFIG}', 'rb') as f:
    cfg = Config(**tomllib.load(f))
print(f'   OK — action_dry_run={cfg.action_dry_run} action_enabled_tiers={cfg.action_enabled_tiers}')
"

# ------------------------------------------------- install/refresh the units
#
# NEVER blindly overwrite an installed unit: found the hard way running this
# script for the first time — neuropaca-soak.service's installed copy had a
# hand-added `Environment=NEUROPACA_SOAK_SKIP_GATE=1` (an operator override
# recorded in data/soak/soak.log, needed to resume an already-running soak
# without re-passing the 1 h gate check) that does not exist in the repo
# template. A silent overwrite deleted it and crash-looped the soak within
# a minute. So: diff the rendered template against what's installed, and
# only write it if there is no installed copy yet, or `--refresh-units` was
# passed explicitly — otherwise warn and leave the installed file alone.
echo "-- installing systemd --user units from scripts/systemd/*.service"
mkdir -p "$UNIT_DIR"
tmp_unit_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_unit_dir"' EXIT
for u in "${UNITS[@]}"; do
    rendered="${tmp_unit_dir}/${u}.service"
    installed="${UNIT_DIR}/${u}.service"
    sed "s|__REPO__|${REPO}|g" "scripts/systemd/${u}.service" >"$rendered"
    if [ ! -f "$installed" ]; then
        cp "$rendered" "$installed"
        echo "   installed ${u}.service (new)"
    elif diff -q "$rendered" "$installed" >/dev/null 2>&1; then
        : # already matches the template — nothing to do
    elif [ "$force_units" -eq 1 ]; then
        cp "$rendered" "$installed"
        echo "   refreshed ${u}.service from the template (--refresh-units) — any local override was dropped"
    else
        echo "   SKIPPED ${u}.service — installed copy differs from the repo template:"
        diff -u "$installed" "$rendered" | sed 's/^/     /' || true  # diff exits 1 on a real diff — not a script error
        echo "     leaving the installed copy alone. Pass --refresh-units to overwrite it."
    fi
done
systemctl --user daemon-reload

# ------------------------------------------------------------------ bring up
# neuropacad: always restart, so a config change actually takes effect (see
# the docstring above) — `enable` alone would leave an already-running
# process on its old config.
echo "-- enabling + restarting neuropacad.service (picks up current neuropaca.toml)"
systemctl --user enable neuropacad.service
systemctl --user restart neuropacad.service

# The soak/tray units don't need a restart to pick up code changes (they
# re-exec their scripts fresh each start; nothing here changed their own
# config), so `enable --now` is enough — it starts them if they aren't
# already running and leaves them alone if they are.
echo "-- enabling the 7-day soak supervisor (continues its existing accrued runtime)"
systemctl --user enable --now neuropaca-soak.service

echo "-- enabling the presence tray"
systemctl --user enable --now neuropaca-tray.service

echo "-- enabling the soak's own tray widget"
systemctl --user enable --now neuropaca-soak-tray.service

# --------------------------------------------------------------------- verify
echo "-- waiting for the daemon to come up"
sleep 5

failed=0
for u in "${UNITS[@]}"; do
    if systemctl --user is-active --quiet "${u}.service"; then
        echo "   OK     ${u}.service"
    else
        echo "   FAILED ${u}.service"
        failed=1
    fi
done

if [ "$failed" -ne 0 ]; then
    echo
    echo "HALT — one or more units failed to come up. Recent neuropacad log:"
    journalctl --user -u neuropacad.service -n 40 --no-pager 2>&1 || true
    exit 1
fi

print_status
echo
echo "=== live ==="
