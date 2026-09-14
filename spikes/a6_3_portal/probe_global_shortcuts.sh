#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>
#
# A6.3 · de-risking spike — does this machine's desktop portal implement
# org.freedesktop.portal.GlobalShortcuts (VISION_PHASES.md A6.3 spike #3)?
#
# Read-only: introspects the running xdg-desktop-portal over the session bus.
# Makes no calls that request, register, or change anything.
#
# Usage: ./probe_global_shortcuts.sh
set -euo pipefail

echo "XDG_CURRENT_DESKTOP=${XDG_CURRENT_DESKTOP:-<unset>}"
echo "XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-<unset>}"
echo

if ! command -v gdbus >/dev/null 2>&1; then
    echo "gdbus not found — cannot probe" >&2
    exit 1
fi

echo "Interfaces exposed at org.freedesktop.portal.Desktop:"
INTERFACES=$(gdbus introspect --session \
    --dest org.freedesktop.portal.Desktop \
    --object-path /org/freedesktop/portal/desktop 2>&1 \
    | grep -oP 'interface \K[a-zA-Z0-9.]+' || true)
echo "$INTERFACES"
echo

if echo "$INTERFACES" | grep -q "^org.freedesktop.portal.GlobalShortcuts$"; then
    echo "RESULT: GlobalShortcuts IS available."
else
    echo "RESULT: GlobalShortcuts is NOT available on this portal."
fi
