"""L7 · quarantine — the reason nothing is ever destroyed (rules.md §5.7).

"No action deletes user data — move to quarantine with a TTL." That applies to
overwrites too: before `FileWriteAction` touches an existing file, its current
bytes are copied here and the copy is what `rollback()` restores from.

Layout, under `Config.quarantine_path`:

    <token>.bin    the preserved bytes
    <token>.json   {origin, stashed_at, expires_at, size}

The sidecar is what makes the store self-describing after a restart — the token
alone does not say where the bytes came from. `purge_expired()` deletes only
pairs whose `expires_at` has passed, and only ever inside the quarantine root.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from neuropaca.core.errors import SafetyGateError

_log = logging.getLogger(__name__)


class Quarantine:
    def __init__(self, root: str | Path, ttl_hours: int) -> None:
        self._root = Path(root).expanduser()
        self._ttl = timedelta(hours=ttl_hours)

    @property
    def root(self) -> Path:
        return self._root

    async def stash(self, path: str | Path) -> str | None:
        """Preserve `path`'s current bytes. Returns a token, or None if the file
        does not exist yet (a first write overwrites nothing, so there is nothing
        to preserve — that is not a failure)."""
        origin = Path(path)
        if not origin.exists():
            return None
        if not origin.is_file():
            raise SafetyGateError(f"refusing to quarantine a non-file: {origin}")
        token = f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid4().hex[:8]}"
        try:
            await asyncio.to_thread(self._copy_in, origin, token)
        except OSError as exc:
            raise SafetyGateError(f"cannot back up {origin} before writing: {exc}") from exc
        _log.info("L7 quarantined %s as %s", origin.name, token)
        return token

    def _copy_in(self, origin: Path, token: str) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self._root.chmod(0o700)
        blob = self._root / f"{token}.bin"
        shutil.copy2(origin, blob)
        now = datetime.now(UTC)
        meta = {
            "origin": str(origin),
            "stashed_at": now.isoformat(),
            "expires_at": (now + self._ttl).isoformat(),
            "size": blob.stat().st_size,
        }
        (self._root / f"{token}.json").write_text(json.dumps(meta), encoding="utf-8")

    async def restore(self, token: str) -> bool:
        """Put a stashed copy back where it came from. False if the pair is gone."""
        try:
            return await asyncio.to_thread(self._copy_out, token)
        except OSError as exc:
            _log.error("quarantine restore of %s failed: %r", token, exc)
            return False

    def _copy_out(self, token: str) -> bool:
        blob = self._root / f"{token}.bin"
        sidecar = self._root / f"{token}.json"
        if not blob.is_file() or not sidecar.is_file():
            return False
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        origin = Path(str(meta["origin"]))
        origin.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(blob, origin)
        return True

    def origin_of(self, token: str) -> str | None:
        sidecar = self._root / f"{token}.json"
        if not sidecar.is_file():
            return None
        try:
            return str(json.loads(sidecar.read_text(encoding="utf-8"))["origin"])
        except (OSError, ValueError, KeyError):
            return None

    async def purge_expired(self, now: datetime | None = None) -> int:
        """Delete expired pairs. Returns how many were swept."""
        return await asyncio.to_thread(self._purge, now or datetime.now(UTC))

    def _purge(self, now: datetime) -> int:
        if not self._root.is_dir():
            return 0
        swept = 0
        for sidecar in self._root.glob("*.json"):
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
                expires = datetime.fromisoformat(str(meta["expires_at"]))
            except (OSError, ValueError, KeyError):
                continue  # unreadable sidecar: leave it alone rather than guess
            if expires > now:
                continue
            blob = sidecar.with_suffix(".bin")
            # Belt and braces: only ever unlink inside our own root.
            for victim in (blob, sidecar):
                if victim.parent == self._root and victim.exists():
                    victim.unlink()
            swept += 1
        if swept:
            _log.info("L7 quarantine swept %d expired entries", swept)
        return swept
