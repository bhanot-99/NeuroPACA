"""
Audit log — independent of tier (see guide.md's "Safety measures"
section: "every resolved action... gets logged through the Audit module,
whether or not it needed confirmation"). Unlike tier GATING, which stays off
until config.SAFETY_TIERS_ENABLED is turned on, this is pure observability
with zero user-facing friction — it's wired in and active immediately,
matching the same principle Step 1's correction layer used ("a correctness/
visibility fix, not a safety gate, happens even with no tiers on").

One JSON line per resolved action: when, what was heard, which skill/args
were chosen, its tier, and the outcome. Append-only, plain text — no
database needed for what is, in practice, a personal activity log.
"""

import json
import os
import time

_LOG_PATH = os.path.expanduser("~/.local/share/voice-standalone/audit.jsonl")


def record(*, text: str, skill_name: str, args: dict, tier: str, outcome: str) -> None:
    os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "heard": text,
        "skill": skill_name,
        "args": args,
        "tier": tier,
        "outcome": outcome,
    }
    try:
        with open(_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # a logging failure must never take down the actual command
