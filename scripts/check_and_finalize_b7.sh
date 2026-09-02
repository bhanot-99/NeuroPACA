#!/usr/bin/env bash
#
# B7 · Exit Criterion 5 — periodic, unattended finalize-check.
#
#   ./scripts/check_and_finalize_b7.sh
#
# Run on a recurring schedule (by neuropaca-b7-finalize-check.timer, every 30
# min once installed by start_b7_soak.sh) rather than a single computed
# deadline — because the soak daemon now survives reboots (it's a persistent
# systemd --user service, not a detached process), the 24 h window is
# measured across however many power-cycles it takes in real calendar time
# (validate_b7_dryrun.py spans first-to-last audit line, not uptime), so
# there is no one instant to schedule a one-shot timer for.
#
# Always exits 0 — a not-ready soak (short window, dirty tree, wrong branch,
# a high-tier proposal awaiting judgement) is the *normal* state on most
# checks, not a failure, so the systemd timer that drives this should never
# show as failed for it. Every attempt is appended to
# data/soak_b7_finalize_check.log so you can see what happened.

cd "$(dirname "$0")/.."

{
    echo "=== finalize-check $(date -Iseconds) ==="
    if ./scripts/finalize_b7.sh; then
        echo ">>> B7 FINALISED — merged into main locally. Review, then: git push origin main"
        echo ">>> The soak daemon is still running (systemctl --user status neuropaca-b7-soak.service)"
        echo ">>> — stop it with: ./scripts/stop_b7_soak.sh"
    else
        echo "(not ready yet — see above)"
    fi
    echo
} >>data/soak_b7_finalize_check.log 2>&1

exit 0
