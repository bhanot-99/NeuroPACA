# S3 Media Collector Spike — MPRIS and Title Pattern Membrane

## Questions Investigated

1. **MPRIS Coverage for Browser Video**: Does MPRIS cover browser-based video playback, or must we hook into the Wayland window title sensing pipeline?
2. **Music vs. Video Trust Levels**: How to handle clean structured metadata (Spotify/VLC) vs. noisy raw web titles (Brave/Chrome) mirroring `document.title` into `xesam:title`?
3. **Allowlist Pattern Membrane**: Can a compact regex pattern set capture real video titles while safely dropping 100% of non-media web browser titles?
4. **Season-Optional Shape**: How to cleanly handle shows and podcasts that have episode numbers but no season numbers?
5. **Read-Only Invariance**: Can all inspection be done strictly read-only over `busctl --user` without invoking mutating D-Bus methods?

---

## Empirical Findings

### 1. MPRIS Already Covers Browser Video Playback
- Brave automatically registers an active `org.mpris.MediaPlayer2.brave.instance<PID>` service on the user session D-Bus whenever a video or audio element is playing.
- Live measurement confirmed:
  ```json
  {
    "service": "org.mpris.MediaPlayer2.brave.instance9888",
    "PlaybackStatus": "Playing",
    "Position": 783516611,
    "Metadata": {
      "mpris:length": 1450111999,
      "xesam:title": "Kurokos Basketball 3 Episode 1 Watch All Episodes at Hianime"
    }
  }
  ```
- **Architectural Simplification**: No need to cross or modify B14's title membrane in `sensing/activity/collector.py`. `MediaIngest` can operate completely decoupled from the Wayland window pipeline by querying MPRIS directly.

### 2. Dual Trust Membrane: Music vs. Video
- **Music (Trusted as-is)**: Genuine music players (Spotify, VLC, local MPRIS players) populate `xesam:artist` and `xesam:album`. When these fields are present, the metadata is structured and trusted directly.
- **Video / Browser (Unstructured / Raw)**: Browsers mirror `document.title` into `xesam:title`, which often contains website junk (e.g. `"Watch All Episodes at Hianime"`, `"English Dub Crunchyroll"`). This raw text must **never** enter GraphMemory verbatim.
- An allowlist pattern membrane (`data/media_title_patterns.default.toml`) filters `xesam:title`:
  - Matches extract `(show, season, episode)`.
  - Unmatched titles are **dropped completely** (zero raw sentence leakage).

### 3. Allowlist Pattern Performance
Tested against a corpus of 12 real media titles and 10 negative non-media/browser titles:
- **Recall on Media Titles**: **12 / 12 (100.0%)** matched and extracted correctly.
- **Drop Rate on Non-Media Titles**: **10 / 10 (100.0%)** dropped. Zero false positives; zero raw titles leaked.
- Supports season-optional shapes (e.g. `"Severance Episode 9"`, `"The Daily - Episode 1420"`, `"Huberman Lab #112"`).

### 4. Reading Scoped Out
MPRIS provides no signal for reading or e-books without heavy external dependencies. In alignment with `VISION_PHASES.md` §0.3 principles, S3 strictly scopes to **watched + listened** media; reading is explicitly excluded.

---

## Go / No-Go Decision

**GO.** Proceed with Phase S3 design:
- `sensing/media_ingest.py` (`MediaIngest`) polling active MPRIS services every 30 seconds.
- `EpisodeKind.MEDIA_SPAN` for playback intervals; `EpisodeKind.MEDIA_POSITION_FACT` for superseding progress facts.
- `NodeType.SERIES` (`series:<slug>`) in `GraphMemory` (schema v10 → v11) under a new `domain:media` hub.
- Briefing continuity candidate in `interface/briefing.py`.
