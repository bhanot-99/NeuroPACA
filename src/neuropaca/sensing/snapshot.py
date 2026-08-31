"""`MetricSnapshot` — one reading from one collector (Architecture.md §4).

L2 is dumb: `anomaly_score` is always `0.0` here. All baselining and scoring is
L3's job (D-7 B5). `data` is a loose dict because each collector reports a
different shape; the snapshot itself is the typed envelope (rules.md §2).

The `METRIC_COLLECTED` event carries exactly `{"snapshot": snapshot}` (D-7 B7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class MetricSnapshot:
    collector_name: str
    timestamp: datetime
    data: dict[str, Any] = field(default_factory=dict)
    anomaly_score: float = 0.0
