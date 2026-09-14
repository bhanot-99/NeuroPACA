#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""The ONLY thing in the whole voice-cloud feature that talks to the
internet (user decision 2026-09-14, VISION_PHASES.md A6.3).

`neuropacad` runs with `PrivateNetwork=true` — it cannot open a network
socket to Google or anywhere else, full stop, enforced by systemd, not just
by the code choosing not to. This script is a completely separate process
(its own systemd unit, `scripts/systemd/neuropaca-voice-cloud.service`),
outside that sandbox, that does exactly one job: watch a folder for audio
clips the daemon dropped, send each one to the Gemini API for
transcription, and drop the answer back into another folder. Same
separation of concerns `plugins/mail/fetcher.py` already established for
mail (S1) — the daemon stays offline, one small host-scoped helper is the
egress point, and it is watched by its own unit so it can be killed,
restarted, or ripped out independently of everything else.

Talks to `sensing/cloud_voice_bridge.py`'s `GeminiBridgeSttBackend` purely
through the filesystem (a `<request_id>.wav` in --pending-dir in, a
`<request_id>.json` in --response-dir out) — there is no other coupling, so
either side can be replaced without touching the other.

The API key is never a config file field (same discipline as
`plugins/mail/fetcher.py`'s `--password-cmd`): it is fetched at process
start from a secret manager via `--api-key-cmd` (e.g.
``secret-tool lookup service neuropaca-gemini``), or NEUROPACA_GEMINI_API_KEY
for a quick manual run — never written to disk, never logged.

USAGE
    scripts/voice_cloud_helper.py \\
        --pending-dir data/voice_cloud/pending \\
        --response-dir data/voice_cloud/responses \\
        --api-key-cmd "secret-tool lookup service neuropaca-gemini"

Every failure mode (missing key, network error, a bad response from Gemini,
an unreadable WAV file) writes ``{"error": "..."}`` to the response file
rather than crashing the loop or leaving the request to time out for no
visible reason — `GeminiBridgeSttBackend` falls back to the local model the
instant it sees either an error response or nothing at all within its
timeout, so a bad key or a network outage just makes voice slower, not
broken (rules.md §2, applied to a script instead of a handler).
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_log = logging.getLogger("voice_cloud_helper")

_POLL_INTERVAL_SECONDS = 0.2
# Verified live 2026-09-14: a real audio call to gemini-3.6-flash took
# anywhere from ~2s to a 10s timeout, plus one observed transient 503 —
# 15s + one retry on 503 gives it a real chance without the caller (this
# helper's own caller is GeminiBridgeSttBackend, with its own separate
# voice_cloud_timeout_seconds budget) waiting indefinitely.
_GEMINI_TIMEOUT_SECONDS = 15.0
_GEMINI_RETRY_DELAY_SECONDS = 1.0
_TRANSCRIBE_PROMPT = (
    "Transcribe the following short voice-command audio clip verbatim, in "
    "English. Output ONLY the transcription text — no commentary, no "
    "quotation marks, no punctuation-only guesses if the audio is silent "
    "or unintelligible (output an empty string in that case)."
)


def _get_api_key(api_key_cmd: str | None) -> str:
    env_key = os.environ.get("NEUROPACA_GEMINI_API_KEY")
    if env_key:
        return env_key
    if api_key_cmd:
        try:
            cmd = shlex.split(api_key_cmd)
            out = subprocess.check_output(cmd, text=True, timeout=15)
            return out.strip()
        except Exception as exc:
            raise RuntimeError(f"API key command failed: {exc}") from exc
    raise ValueError("No Gemini API key provided. Set --api-key-cmd or NEUROPACA_GEMINI_API_KEY.")


def _call_gemini_transcribe(audio_bytes: bytes, api_key: str, model: str) -> str:
    """One REST call to Gemini's `generateContent` endpoint with the audio
    inlined as base64 `audio/wav`. Raises on any HTTP/network/shape error —
    the caller turns that into an `{"error": ...}` response file, never lets
    it propagate into the poll loop.

    The key goes in the `x-goog-api-key` HEADER, never a `?key=` query
    parameter — found the hard way (2026-09-14): `requests` embeds the full
    request URL (params included) in its own exception messages, so a
    query-string key leaks into any `{"error": str(exc)}` response file this
    helper writes, and into any log line built from that exception. A header
    never appears in `requests`'s exception text or in `resp.url`, so the
    same "log the exception" discipline the rest of this codebase relies on
    (rules.md §2) can't leak it here even by accident.
    """
    import requests

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": api_key}
    body = {
        "contents": [
            {
                "parts": [
                    {"text": _TRANSCRIBE_PROMPT},
                    {
                        "inline_data": {
                            "mime_type": "audio/wav",
                            "data": base64.b64encode(audio_bytes).decode("ascii"),
                        }
                    },
                ]
            }
        ]
    }

    # One retry on a 503 only (Google's own "temporarily overloaded" signal,
    # observed live 2026-09-14) — never on a 4xx (a bad request/key retrying
    # would just fail the same way again) and never more than once, so a
    # sustained outage still surfaces to the caller within a bounded time
    # instead of doubling this function's worst-case latency indefinitely.
    for attempt in range(2):
        resp = requests.post(url, headers=headers, json=body, timeout=_GEMINI_TIMEOUT_SECONDS)
        if resp.status_code == 503 and attempt == 0:
            time.sleep(_GEMINI_RETRY_DELAY_SECONDS)
            continue
        break
    resp.raise_for_status()
    data = resp.json()

    candidates = data.get("candidates") or []
    if not candidates:
        # No candidate at all usually means the prompt itself was blocked
        # (promptFeedback.blockReason) before any generation happened.
        reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates returned")
        raise RuntimeError(f"Gemini returned no transcription: {reason}")

    parts = candidates[0].get("content", {}).get("parts")
    if not parts:
        # A candidate with no parts (e.g. finishReason=SAFETY/RECITATION with
        # empty content) — same "clear reason, not a bare KeyError" goal.
        finish_reason = candidates[0].get("finishReason", "unknown")
        raise RuntimeError(f"Gemini returned an empty response (finishReason={finish_reason})")

    return str(parts[0].get("text", "")).strip()


def _write_response(response_dir: Path, request_id: str, payload: dict[str, Any]) -> None:
    # Atomic write, same convention as the request side
    # (cloud_voice_bridge.py's _write_request): the bridge's poll loop must
    # never see a partially-written response file.
    final_path = response_dir / f"{request_id}.json"
    tmp_path = response_dir / f"{request_id}.json.tmp"
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    tmp_path.rename(final_path)


def _process_one(wav_path: Path, response_dir: Path, api_key: str, model: str) -> None:
    request_id = wav_path.stem
    try:
        audio_bytes = wav_path.read_bytes()
        transcript = _call_gemini_transcribe(audio_bytes, api_key, model)
        _write_response(response_dir, request_id, {"transcript": transcript})
    except Exception as exc:
        _log.warning("voice_cloud_helper: request %s failed: %s", request_id, exc)
        _write_response(response_dir, request_id, {"error": str(exc)})
    finally:
        wav_path.unlink(missing_ok=True)


def run(pending_dir: Path, response_dir: Path, api_key_cmd: str | None, model: str) -> None:
    pending_dir.mkdir(parents=True, exist_ok=True)
    response_dir.mkdir(parents=True, exist_ok=True)
    api_key = _get_api_key(api_key_cmd)
    _log.info("voice_cloud_helper: watching %s (model=%s)", pending_dir, model)
    while True:
        for wav_path in sorted(pending_dir.glob("*.wav")):
            _process_one(wav_path, response_dir, api_key, model)
        time.sleep(_POLL_INTERVAL_SECONDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pending-dir", required=True, type=Path)
    parser.add_argument("--response-dir", required=True, type=Path)
    parser.add_argument("--api-key-cmd", default=None, help="Shell command that prints the API key")
    # Verified live 2026-09-14: gemini-2.0-flash (the earlier default here)
    # is retired — Google's own 404 on that model recommends this one.
    # Re-check scripts/systemd/neuropaca-voice-cloud.service's --model flag
    # too if this ever needs to change again; the two are set independently.
    parser.add_argument("--model", default="gemini-3.6-flash")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        run(args.pending_dir, args.response_dir, args.api_key_cmd, args.model)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        _log.error("voice_cloud_helper: fatal — %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
