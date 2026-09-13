#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""S3 Media Collector Spike — MPRIS and Title Pattern Extraction Membrane.

Investigates:
1. Live MPRIS sessions via busctl list and GetAll org.mpris.MediaPlayer2.Player.
2. Video vs Music trust levels:
   - Music (xesam:artist / xesam:album populated): trusted structured metadata.
   - Video (raw xesam:title mirrored from browser document.title): raw, noisy,
     must pass through an allowlist pattern membrane or be dropped entirely.
3. Clean omission of absent fields (season-optional shape: show + episode with no season).
4. Drop rate measurement on positive media titles vs negative non-media/browser titles.
5. Read-only verification: collector only queries `list` and `GetAll`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

# Common web streaming suffixes to strip before or during extraction
CLEANUP_SUFFIXES = re.compile(
    r"\s*[-|•:]?\s*(?:Watch\s+All\s+Episodes.*|Watch\s+Online.*|Full\s+Episode.*|"
    r"Hianime.*|Crunchyroll.*|Netflix.*|YouTube.*|Funimation.*|AnimePahe.*)$",
    re.IGNORECASE,
)

# Standard video/series title patterns
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 1. Show - S02E07 or Show S2 E7 or Show s02e07
    (
        "s_e_compact",
        re.compile(
            r"^(?P<show>.+?)\s+[-_:]?\s*[sS](?P<season>\d+)\s*[:._-]?\s*[eE](?P<episode>\d+)",
            re.IGNORECASE,
        ),
    ),
    # 2. Show Season 2 Episode 7
    (
        "season_episode_verbose",
        re.compile(
            r"^(?P<show>.+?)\s+[-_:]?\s*[sS]eason\s*(?P<season>\d+)\s*[-_:]?\s*[eE]pisode\s*(?P<episode>\d+)",
            re.IGNORECASE,
        ),
    ),
    # 3. Show <SeasonNum> Episode <EpisodeNum> (e.g., Example Anime 3 Episode 1)
    (
        "show_num_episode_num",
        re.compile(
            r"^(?P<show>[A-Za-z0-9\s'\-]+?)\s+(?P<season>\d+)\s+[-_:]?\s*[eE]pisode\s*(?P<episode>\d+)",
            re.IGNORECASE,
        ),
    ),
    # 4. Show Episode 7 (season-optional)
    (
        "episode_only_verbose",
        re.compile(
            r"^(?P<show>.+?)\s+[-_:]?\s*[eE]pisode\s*(?P<episode>\d+)",
            re.IGNORECASE,
        ),
    ),
    # 5. Show Ep 7 / Show Ep. 7
    (
        "ep_compact",
        re.compile(
            r"^(?P<show>.+?)\s+[-_:]?\s*[eE][pP]\.?\s*(?P<episode>\d+)",
            re.IGNORECASE,
        ),
    ),
    # 6. Podcast / numbered episode: Show #112
    (
        "hash_episode",
        re.compile(
            r"^(?P<show>.+?)\s+[-_:]?\s*#(?P<episode>\d+)",
            re.IGNORECASE,
        ),
    ),
]


@dataclass(frozen=True, slots=True)
class ExtractedMedia:
    media_type: str  # "video" | "music"
    show_or_artist: str
    title: str
    season: int | None = None
    episode: int | None = None
    album: str | None = None
    position_seconds: float = 0.0
    duration_seconds: float | None = None


def extract_from_metadata(metadata: dict[str, Any], position_us: int = 0) -> ExtractedMedia | None:
    # Handle D-Bus variant unwrapping if needed
    def _unwrap(v: Any) -> Any:
        if isinstance(v, dict) and "data" in v:
            return v["data"]
        return v

    meta = {k: _unwrap(v) for k, v in metadata.items()}

    # 1. Branch on trust: Music check
    # Genuine music players populate xesam:artist or xesam:album
    raw_artists = meta.get("xesam:artist")
    raw_album = meta.get("xesam:album")
    raw_title = str(meta.get("xesam:title") or "").strip()

    artists: list[str] = []
    if isinstance(raw_artists, list):
        artists = [str(a).strip() for a in raw_artists if str(a).strip()]
    elif isinstance(raw_artists, str) and raw_artists.strip():
        artists = [raw_artists.strip()]

    album = str(raw_album).strip() if raw_album else None

    # Length in microseconds -> seconds
    length_us = meta.get("mpris:length")
    duration_sec = float(length_us) / 1_000_000.0 if length_us and length_us > 0 else None
    position_sec = float(position_us) / 1_000_000.0 if position_us > 0 else 0.0

    if (artists or album) and raw_title:
        # Music stream
        artist_str = ", ".join(artists) if artists else "Unknown Artist"
        return ExtractedMedia(
            media_type="music",
            show_or_artist=artist_str,
            title=raw_title,
            album=album,
            position_seconds=position_sec,
            duration_seconds=duration_sec,
        )

    # 2. Video / Browser stream: parse raw_title through pattern membrane
    if not raw_title:
        return None

    # Strip web streaming junk suffix
    cleaned_title = CLEANUP_SUFFIXES.sub("", raw_title).strip()

    for _pattern_name, pattern in PATTERNS:
        match = pattern.search(cleaned_title)
        if match:
            groups = match.groupdict()
            show = groups.get("show", "").strip(" -:_")
            season_str = groups.get("season")
            episode_str = groups.get("episode")

            season = int(season_str) if season_str and season_str.isdigit() else None
            episode = int(episode_str) if episode_str and episode_str.isdigit() else None

            if show and episode is not None:
                return ExtractedMedia(
                    media_type="video",
                    show_or_artist=show,
                    title=cleaned_title,
                    season=season,
                    episode=episode,
                    position_seconds=position_sec,
                    duration_seconds=duration_sec,
                )

    # No pattern matched: DROP completely. Never store raw sentence in the graph!
    return None


async def run_busctl(args: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "busctl",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode or 0, stdout.decode("utf-8", errors="replace")


async def inspect_live_mpris() -> list[dict[str, Any]]:
    rc, out = await run_busctl(["--user", "--json=short", "list"])
    if rc != 0:
        return []

    services = json.loads(out)
    mpris_services = [
        s
        for s in services
        if s.get("name", "").startswith("org.mpris.MediaPlayer2.") and s.get("pid") is not None
    ]

    captured: list[dict[str, Any]] = []
    for s in mpris_services:
        dest = s["name"]
        rc, out_props = await run_busctl(
            [
                "--user",
                "--json=short",
                "call",
                dest,
                "/org/mpris/MediaPlayer2",
                "org.freedesktop.DBus.Properties",
                "GetAll",
                "s",
                "org.mpris.MediaPlayer2.Player",
            ]
        )
        if rc == 0:
            parsed = json.loads(out_props)
            # data is array with property dict
            props = parsed.get("data", [{}])[0]
            captured.append(
                {
                    "service": dest,
                    "pid": s.get("pid"),
                    "process": s.get("process"),
                    "properties": props,
                }
            )
    return captured


DEFAULT_MEDIA_POSITIVES = [
    "Example Anime 3 Episode 1 Watch All Episodes at Hianime",
    "Attack on Titan - S04E28 - The Dawn of Humanity",
    "Breaking Bad S02E07 - Negro y Azul",
    "The Office (US) Season 3 Episode 5",
    "Stranger Things 2 Episode 7",
    "Severance Episode 9",
    "The Daily - Episode 1420",
    "Huberman Lab #112 - Sleep Toolkit",
    "Chainsaw Man Episode 12 - English Dub Crunchyroll",
    "Arcane Season 1 Episode 9 The Monster You Created",
    "Better Call Saul S06E13",
    "Dark Season 3 Episode 8 - The Paradise",
]

DEFAULT_NON_MEDIA_NEGATIVES = [
    "YouTube - Home",
    "Google Search - how to make pizza",
    "Reddit: the front page of the internet",
    "Twitch - Following",
    "Inbox (12) - user@example.com",
    "GitHub - bhanot-99/NeuroPACA: A local cognitive architecture",
    "Hianime - Watch Anime Online Free",
    "Netflix - Browse",
    "Spotify Web Player",
    "Python 3.12 Documentation",
]


def evaluate_patterns_on_corpus(
    positives: list[str] | None = None,
    negatives: list[str] | None = None,
) -> dict[str, Any]:
    media_positives = positives if positives is not None else DEFAULT_MEDIA_POSITIVES
    non_media_negatives = negatives if negatives is not None else DEFAULT_NON_MEDIA_NEGATIVES

    pos_results = []
    for title in media_positives:
        fake_meta = {
            "xesam:title": title,
            "xesam:artist": [""],
            "xesam:album": "",
            "mpris:length": 1440000000,
        }
        res = extract_from_metadata(fake_meta, position_us=120000000)
        pos_results.append(
            {
                "raw_title": title,
                "matched": res is not None,
                "extracted": asdict(res) if res else None,
            }
        )

    neg_results = []
    for title in non_media_negatives:
        fake_meta = {
            "xesam:title": title,
            "xesam:artist": [""],
            "xesam:album": "",
            "mpris:length": 0,
        }
        res = extract_from_metadata(fake_meta, position_us=0)
        neg_results.append(
            {
                "raw_title": title,
                "dropped": res is None,
                "leaked": res is not None,
            }
        )

    pos_matched = sum(1 for r in pos_results if r["matched"])
    neg_dropped = sum(1 for r in neg_results if r["dropped"])

    recall = pos_matched / len(media_positives) if media_positives else 1.0
    drop_rate = neg_dropped / len(non_media_negatives) if non_media_negatives else 1.0

    return {
        "media_positives": {
            "total": len(media_positives),
            "matched": pos_matched,
            "recall": recall,
            "results": pos_results,
        },
        "non_media_negatives": {
            "total": len(non_media_negatives),
            "dropped": neg_dropped,
            "drop_rate": drop_rate,
            "results": neg_results,
        },
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="S3 Media Collector Spike")
    parser.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help="Optional JSON file with 'positives' and 'negatives' string lists",
    )
    parser.add_argument(
        "--skip-live",
        action="store_true",
        help="Skip live MPRIS busctl scan",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "summary.json",
        help="Summary JSON output path",
    )
    args = parser.parse_args()

    print("=== S3 Media Collector Spike ===")

    # 1. Live inspection
    live_sessions: list[dict[str, Any]] = []
    if not args.skip_live:
        live_sessions = await inspect_live_mpris()
        print(f"Active MPRIS players discovered: {len(live_sessions)}")
        for s in live_sessions:
            svc = s["service"]
            proc = s["process"]
            props = s["properties"]
            status = props.get("PlaybackStatus", {}).get("data")
            pos = props.get("Position", {}).get("data", 0)
            meta = props.get("Metadata", {}).get("data", {})
            title = meta.get("xesam:title", {}).get("data", "")
            print(f"\n[Live Player] {svc} (PID {s['pid']} / {proc})")
            print(f"  PlaybackStatus: {status} | Position: {pos} μs ({pos / 1e6:.1f} s)")
            print(f'  xesam:title: "{title}"')

            extracted = extract_from_metadata(meta, position_us=pos)
            if extracted:
                print(
                    f"  -> EXTRACTED ({extracted.media_type}): "
                    f"show='{extracted.show_or_artist}', "
                    f"season={extracted.season}, "
                    f"episode={extracted.episode}"
                )
            else:
                print("  -> DROPPED (no allowlist pattern match)")

        if live_sessions:
            live_file = ROOT / "captured_mpris.json"
            live_file.write_text(json.dumps(live_sessions, indent=2), encoding="utf-8")
            print(f"\nSaved live session capture to {live_file}")
    else:
        print("Skipping live MPRIS busctl scan (--skip-live)")

    # 2. Corpus pattern evaluation
    custom_pos = None
    custom_neg = None
    if args.corpus is not None and args.corpus.is_file():
        corpus_data = json.loads(args.corpus.read_text("utf-8"))
        custom_pos = corpus_data.get("positives")
        custom_neg = corpus_data.get("negatives")
        print(f"Loaded custom corpus from {args.corpus}")

    eval_res = evaluate_patterns_on_corpus(positives=custom_pos, negatives=custom_neg)
    pos = eval_res["media_positives"]
    neg = eval_res["non_media_negatives"]
    print("\nCorpus Pattern Evaluation:")
    print(
        f"  Media titles matched (recall): {pos['matched']}/{pos['total']} "
        f"({pos['recall'] * 100:.1f}%)"
    )
    print(
        f"  Non-media titles dropped (protection): {neg['dropped']}/{neg['total']} "
        f"({neg['drop_rate'] * 100:.1f}%)"
    )

    summary = {
        "live_mpris_sessions_count": len(live_sessions),
        "corpus_evaluation": eval_res,
        "empirical_findings": [
            "Brave publishes org.mpris.MediaPlayer2.brave.* directly on media playback.",
            "Music players (Spotify, VLC) provide clean xesam:artist/album - trustworthy as-is.",
            "Browser video mirrors window/tab title with junk suffixes.",
            "Season-optional shape: show + episode without season is supported cleanly.",
            f"Precision on non-media browser titles: {neg['drop_rate'] * 100:.1f}% dropped.",
        ],
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Saved spike summary to {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
