# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""L7 · Action — every effect the daemon can have, behind one gate
(Architecture.md §11b, B7)."""

from neuropaca.action.actions import (
    AdjustBrightnessAction,
    AdjustVolumeAction,
    FileWriteAction,
    MemoryWriteAction,
    NotificationAction,
    OpenAppAction,
    RunCommandAction,
)
from neuropaca.action.audit import ActionAudit
from neuropaca.action.base import ActionResult, ActionTier, BaseAction
from neuropaca.action.confirm import ConfirmationBroker, PendingConfirmation
from neuropaca.action.executor import ActionExecutor
from neuropaca.action.gate import SafetyGate
from neuropaca.action.quarantine import Quarantine
from neuropaca.action.sandbox import CommandOutcome, Sandbox

__all__ = [
    "ActionAudit",
    "ActionExecutor",
    "ActionResult",
    "ActionTier",
    "AdjustBrightnessAction",
    "AdjustVolumeAction",
    "BaseAction",
    "CommandOutcome",
    "ConfirmationBroker",
    "FileWriteAction",
    "MemoryWriteAction",
    "NotificationAction",
    "OpenAppAction",
    "PendingConfirmation",
    "Quarantine",
    "RunCommandAction",
    "SafetyGate",
    "Sandbox",
]

# gen-ref: 16a54c48
