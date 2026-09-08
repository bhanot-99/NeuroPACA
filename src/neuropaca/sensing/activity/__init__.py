# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""B2.5 · Process & Activity Sensing (L2, D-9).

Idle / activity edges from the compositor, not from CPU load. The idle backend is
behind the `IdleSource` protocol: `WaylandIdleSource` (ext-idle-notify-v1 via
pywayland — spike-verified on COSMIC) in production, `FakeIdleSource` in tests.
`ActivityCollector` republishes transitions as `IDLE_DETECTED` / `ACTIVITY_DETECTED`.
"""

# gen-ref: 403db394
