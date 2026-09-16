# Voice Standalone — Architecture

Standalone voice-command assistant with full system access (open apps, web/YouTube
search, volume, brightness, terminal execution). Built outside NeuroPaca on purpose —
integrate back in later, once it works on its own. This document is the plan; nothing
in here is built yet except what Phase 0 already covers.

Uncompromised goals for the decision pipeline: **accuracy** understanding what was
said, **millisecond-scale execution** once it's understood, and **correct** execution
(never run a best guess). Safety/tier gating is a separate, later concern — its own
section near the end, deliberately not entangled with the pipeline design.

## Where each idea in this doc came from

| Idea | Source |
|---|---|
| Skills-first, LLM only as fallback | Talon Voice (grammar mode vs. dictation mode); Mycroft/OVOS (Adapt + Padatious + fallback pipeline) |
| Never execute your best guess — correct it first | Home Assistant Assist (fuzzy-matches a misheard slot against the real entity list, rewrites the sentence, executes the *corrected* version) |
| Pause → let the user edit → resume → feed real output back in | Open Interpreter's pre-Rust confirmation loop (a generator that yields before running code, re-reads from shared state on resume) |
| Editable preview instead of yes/no | ai-shell-agent / llm-tools-execute-shell |
| Machine-scan a command before showing it | Open Interpreter's safe-mode semgrep pass |
| Turn-detection (know when you've stopped talking, without a flat timer) | LiveKit Agents' adaptive interruption/turn-taking |
| Skills as a scalable local layer (150+) | Your friend's local-model build — same shape as Mycroft/OVOS's Adapt+Padatious pipeline, independently arrived at |
| Local semantic matching via vector similarity instead of LLM-per-utterance | Padatious's own approach (a small trained matcher, not a full LLM call) generalized with off-the-shelf sentence embeddings |
| English + Hindi TTS with real, verified Hindi voices | Kokoro-82M (`hexgrad/Kokoro-82M`, Apache-2.0) — re-verified directly against its own `VOICES.md`, not trusted from the old A6 feature's memory |
| ~~Punjabi TTS~~ (built, benchmarked, removed — see Step 7) | AI4Bharat's IndicF5 (MIT) — an IIT Madras research group building open Indic-language speech models specifically; live full-pipeline latency measured 96s/phrase on CPU, unusable |
| Indian-accented English (not just Hindi) | ~~MeloTTS~~ (tried, dropped — only one voice, no way to tune/swap it) → Piper's `en_IN-spicor` (NavGurukul AI Labs) — verified directly on Hugging Face, 63.5MB, CPU-only; Google Cloud TTS's Chirp3-HD (30 real en-IN voices) as the cloud fallback if it disappoints |
| Streaming/low-latency TTS, <100ms time-to-first-audio | RealtimeTTS (KoljaB) — has a built-in Kokoro engine |
| Concrete techniques for humanizing speech (not just model choice) | ElevenLabs' own engineering blog on sounding less robotic, converged with multiple independent TTS guides |

## Phase 0 — already scaffolded (done)

`audio_capture.py` (push-to-talk WAV recording), `stt.py` (Gemini transcription),
`llm_intent.py` (Gemini function-calling over 5 actions), `actions.py` (the 5
executors), `skills.py` (a first, small local fast-path for 4 of the 5, regex-only).
This phase proved the shape works end to end. Everything below replaces/expands it —
`skills.py`'s plain "first regex wins" gets a real confidence layer under it.

## The decision pipeline

```
                         transcribed text
                                │
                                ▼
                ┌───────────────────────────────┐
                │ LAYER 0 — Grammar match         │   ~0.3ms worst case
                │ (Talon-style regex/keyword,     │   (measured: 181 skills,
                │  one per skill, exact structure)│    linear scan)
                └───────────────────────────────┘
                                │
                     hit? ──yes──▶ confidence = 1.0 → EXECUTE
                                │
                               no
                                ▼
                ┌───────────────────────────────┐
                │ LAYER 1 — Local semantic match  │   ~2-5ms
                │ cosine similarity: utterance    │   (one matrix multiply,
                │ vector vs. precomputed example- │    no network call)
                │ phrase vectors, all 181 skills  │
                └───────────────────────────────┘
                                │
                    score ≥ 0.82 ──yes──▶ EXECUTE (confident local match)
                                │
                        0.60 ≤ score < 0.82
                                │
                                ▼
                ┌───────────────────────────────┐
                │ blend with a second cheap signal│
                │ (token-overlap ratio); re-check │
                │ combined score against 0.82     │
                └───────────────────────────────┘
                          │              │
                   accepted│              │still below 0.82
                           ▼              ▼
                        EXECUTE   ┌───────────────────────────────┐
                                  │ LAYER 2 — LLM fallback (Gemini) │  ~300-1500ms
                                  │ only reached when local math    │  (network —
                                  │ isn't confident: genuinely novel│   the real
                                  │ phrasing, multi-step reasoning  │   bottleneck)
                                  └───────────────────────────────┘
                                                │
                                                ▼
        ┌──────────────────────────────────────────────────────────────┐
        │ CORRECTION LAYER — any argument naming a real thing            │
        │ (app, site, file): substring match first (instant) → else      │
        │ 0.6×edit-distance-ratio + 0.4×token-overlap → accept only above│
        │ threshold → else "unresolved," force a fallback, never guess   │
        └──────────────────────────────────────────────────────────────┘
                                                │
                                                ▼
                                  EXECUTE (dict dispatch, O(1);
                                  real cost is the OS call itself:
                                  10-50ms to launch an app, <10ms
                                  for pactl/brightnessctl)
```

**Layer 1, in plain terms:** instead of asking an LLM to compare your sentence
against skills one at a time (181 network round trips — absurd), the sentence and
every skill's example phrases are converted to vectors, and similarity is checked
via a single matrix multiplication. That's a few milliseconds of local arithmetic,
no network, no waiting — this is the concrete answer to "does regex+priority hold
up at 181 skills, or do we need embedding-based routing": yes, this layer is why.

**Honest cost:** Layer 1 needs a small local sentence-embedding model as a new
dependency. Fully local/offline, still millisecond-fast per utterance — but
benchmarked live on this machine (Step 2), `sentence-transformers` (the common
default, torch-based) took ~15s to import+load even warm, ~52s cold — a real
startup cost the original plan didn't account for. `fastembed` (ONNX-based, no
torch) loads in ~1.3s warm with near-identical per-utterance latency (~12ms vs
~10-15ms) — that's what's actually used, model `BAAI/bge-small-en-v1.5`.

## Latency budget

| Stage | Estimated (pipeline design) | Measured (Step 2, real pipeline) |
|---|---|---|
| STT (Gemini, cloud, unavoidable today) | ~300-1500ms | not re-measured — still the real bottleneck |
| Layer 0 grammar scan | <1ms | **0.71ms** — confirmed |
| Layer 1 semantic match (when it fires) | ~2-5ms | **~50ms** — higher than estimated, still imperceptible |
| Layer 1 one-time startup (model load + index build) | not estimated | **~1.9s**, paid once at program start, not per-utterance |
| Correction layer | <1ms | not separately re-measured |
| Execute (dispatch + OS call) | 10-50ms | not re-measured (see Step 1's live executor checks) |
| **Total, after transcription, skill-resolved path** | ~10-60ms | **~1-60ms depending on which layer resolves it** |
| Fallback path (adds one more Gemini call) | + ~300-1500ms | not re-measured |

Everything *after* transcription is still comfortably imperceptible to a human,
but the original ~2-5ms guess for Layer 1 was optimistic — real encode-plus-match
cost came in around 50ms. Worth knowing honestly rather than repeating the
estimate as if it were confirmed. STT remains the one piece that can't hit
millisecond scale yet — the Gemini Live API / local-Whisper option discussed
earlier stays the only remaining lever on total latency.

## Skill catalog — v1 target (181 skills)

Curated by hand against real precedent: 78 skill names pulled from the archived
`MycroftAI/mycroft-skills` collection and 36 from the live OpenVoiceOS skill
store, filtered down to what fits a Linux desktop assistant with real system
access (dropped: hardware-specific speaker skills, IoT/smart-bulb control with
no hardware to control, KDE-only tools, and a literal WiFi-cracking skill that
has no place here) — then filled out to full coverage for categories neither
collection handled well (system control, files, dev tools). C2/C3 come from
NeuroPaca's own `data/webapp_map.default.toml` (the real 27 sites it already
tracks for the graph), not guessed.

Legend: 🔒 = dangerous-tier candidate once tiers exist (see Safety section below)
· ⚙️ = needs an external API/account before it can work at all (not just code) ·
unmarked = buildable now with tools already available (`pactl`, `brightnessctl`,
`xdg-open`, `subprocess`, filesystem calls).

**A. System & power control (24)** — volume up/down/mute/set%, brightness
up/down/set%, dark/light theme toggle, lock screen, shut down 🔒, restart 🔒,
log out, sleep, screenshot, start/stop screen recording, Wi-Fi toggle,
Bluetooth toggle, Do Not Disturb toggle, airplane mode toggle, night light
toggle, battery status, keyboard backlight toggle, external display
toggle/mirror, mic mute/unmute.

**B. App & window management (10)** — open app, close/quit app, switch to app,
list open apps, switch workspace, show desktop/minimize all, maximize current
window, force-quit unresponsive app 🔒, open app launcher, reopen last closed
app.

**C1. Web search & general knowledge (22)** — Google search, YouTube search,
Wikipedia, Wolfram Alpha ⚙️, DuckDuckGo, current weather ⚙️, forecast ⚙️, news
headlines ⚙️, define word, spell word, movie/actor info ⚙️, date, time,
timezone conversion, currency conversion ⚙️, unit conversion, calculator,
stock price ⚙️, my IP address, speed test, "today in history", translate text
⚙️.

**C2. Search your known sites (14)** — search: Gmail ⚙️, LinkedIn, GitHub,
Stack Overflow, Google Drive ⚙️, Reddit, Crunchyroll, Hotstar/JioHotstar,
Hianime, Kayoanime, Comick, Comix, MoviesMod, Modlist. (Same 27-site list
NeuroPaca tracks in `webapp_map.default.toml` — kept as an independent copy
here, not a shared import, per the decoupling decision.)

**C3. Open a known web app (9)** — open: Notion ⚙️, Linear ⚙️, Figma ⚙️,
Google Docs ⚙️, Google Sheets ⚙️, Google Slides ⚙️, ChatGPT, Google Gemini,
Claude. (These just open the site — none of ChatGPT/Gemini/Claude's public web
UIs take a prefilled question via URL.)

**D. Media & entertainment playback (14)** — play/pause, next/previous track,
play on YouTube Music, play a YouTube video, play on Spotify ⚙️, play a
SoundCloud track, play a Bandcamp track, play internet radio, set media
volume, take a photo (webcam), open camera app, change wallpaper, browse
local media by voice.

**E. Files & filesystem (16)** — find file, open file, open folder, list
files, create folder, rename file 🔒, copy file, move file 🔒, delete file 🔒,
compress to archive, extract archive, check disk space, empty trash 🔒, open
recent downloads, search file contents, open file manager at a location.

**F. Terminal & dev tools (14)** — run a shell command 🔒, git status, check
running processes, kill process by name 🔒, check port usage, ping a host,
check CPU usage, check memory usage, check system load, open terminal at a
folder, check package version, check for updates, install/update packages 🔒,
restart a service 🔒.

**G. Productivity & reminders (16)** — set alarm, set timer, set reminder, add
calendar event ⚙️, list today's events ⚙️, add to-do, list to-dos, mark to-do
done, take a note, read back last note, read clipboard aloud, copy text to
clipboard, draft email ⚙️, send email ⚙️, check unread email count ⚙️,
suggest/manage a meal list.

**H. Communication (6)** — send Telegram message ⚙️, read latest chat messages
⚙️, start continuous dictation to file, voice note-to-self, check missed
notifications, read a document aloud.

**I. Fun & personality (16)** — tell a joke, Chuck Norris joke, laugh on
demand, flip a coin, roll a dice, pick randomly from a list, random yes/no
decision, fortune/crystal-ball answer, tell a short story, trivia question,
pop-culture Easter egg quote, repeat what I said (parrot), Pokémon info ⚙️,
cocktail recipe, count to a number, "tell me about yourself."

**J. Assistant meta/fallback (8)** — LLM fallback for unmatched requests,
graceful "I don't understand," announce ready on startup, sleep/stop
listening, wake back up, report version/health status, repeat last response,
cancel current action.

**K. Network & connectivity (6)** — current Wi-Fi network name, connect to
known Wi-Fi, VPN status ⚙️, toggle VPN ⚙️, am I online, list paired Bluetooth
devices.

**L. System maintenance (6)** — battery health, clear app cache, check
uptime, list startup apps, find large files, system info summary.

## Module boundaries (modular monolith)

One process, clean interfaces, no shared mutable state, arrows point one way —
this is what actually stops a change to one piece from corrupting another
(not a network hop; see the earlier conversation on process-vs-module isolation).
Any module can be promoted to a real separate process later without the others
noticing, because the contract doesn't change.

- **Capture** → hands off a raw audio file path. Nothing else. First candidate
  to become a real standalone process once wake-word needs to run continuously.
- **STT** → `transcribe(audio_path) -> str`. Doesn't know skills exist.
- **Skills + Intent** → `resolve(text) -> Action` (name + args). Implements the
  full Layer 0 → 1 → 2 pipeline above. Doesn't know how actions execute or what
  a mic is.
- **Action Execution** → `run(Action) -> Result`. Doesn't know or care where the
  Action came from. Isolated here on purpose — it's the piece with real system
  access, which is also where tier-gating attaches later (see below).
- **TTS** → `speak(text) -> None`. Silent/no-op for now; pluggable later.
- **Audit Log** → `record(event) -> None`, called by every other module,
  depended on by none of them.

## Build order

1. Categories A, B, C1 first — no external accounts needed, Phase 0 already
   partly proves this shape.
2. Layer 0 (grammar) for all 181 skills before Layer 1 (semantic) — get the
   deterministic path solid first, then layer the embedding match underneath
   it as the fuzzy fallback, not the other way around.
3. ⚙️-tagged skills (email, calendar, weather, Spotify, Telegram, etc.) are a
   later wave — each needs its own API/account setup before it can work at
   all, so they don't block anything else.
4. 🔒-tagged skills are wired to their real action from day one (no gating
   yet, per your explicit call) — they're exactly the set the Safety section
   below will wrap once tiers are switched on.
5. Wake word + turn detection (Phase 3, below) only after push-to-talk +
   Layer 0/1/2 are solid and actually used daily.

## Phase 3 — Wake word + turn detection (after the pipeline above is solid and daily-used)

- Add hotword detection (openwakeword or similar) inside the **Capture**
  module so a spoken trigger starts recording, no Enter needed. Push-to-talk
  stays available as a manual fallback always — it doesn't get removed.
- For wake-word sessions specifically, replace "press Enter to stop" with a
  LiveKit-style turn-detection model — reads *how* you're speaking (pace,
  intonation, pauses) to guess you're actually done, instead of a flat silence
  timer that either cuts you off or waits too long. Push-to-talk keeps manual
  Enter-to-stop since that's already unambiguous.

---

## Safety measures — for later, once the pipeline above works reliably

Deliberately separate from everything above. Nothing here is active yet;
this is the plan for when you decide to switch it on, tier by tier.

**Static tiers, assigned per skill at definition time** — never inferred at
runtime, so it's predictable and auditable:

- **SAFE** — read-only or trivially reversible (open app, search,
  volume/brightness, media, information lookups). Executes immediately, no
  prompt — but still logged.
- **REVIEW** — changes state but is recoverable (rename/move a file, install a
  package, restart a service). Shows the resolved action briefly before
  proceeding; a short cancel window, not a hard stop.
- **DANGEROUS** — destructive or high blast-radius (delete file, `run_terminal`,
  shutdown/restart, kill process, empty trash, package updates — the 🔒 items
  in the catalog above). Always routes through the full confirm-loop.

**The confirm-loop mechanism** (Open Interpreter's pattern, adapted): the
executor for anything DANGEROUS becomes a generator. It yields the exact
resolved command before running it; whatever's watching (the CLI loop, later
maybe a tray) can rewrite that value while it's paused; on resume, the loop
re-reads from shared state, not the original guess. Real stdout/stderr feeds
back into the next reasoning step, so a session can chain ("now check if that
worked") instead of being one-shot.

- **Editable preview, not yes/no** — the pause step shows the literal
  command, pre-filled and editable. Fixes a misheard filename without
  starting over.
- **Machine-scan tripwire** — before the preview is even shown, run the
  resolved command through a cheap pattern list (`rm -rf`, `dd`, `mkfs`,
  `> /dev/`, `curl | sh`, `sudo`, `kill -9`, etc.) and flag it visibly if it
  hits — a second check that doesn't rely on catching it by eye on a skim.
  `run_terminal` is DANGEROUS by default always; the scan only decides how
  loudly it warns within that gate, not whether the gate applies.

**Audit trail, independent of tier** — every resolved action (transcribed
input, chosen skill/args, tier, outcome) gets logged through the Audit module,
whether or not it needed confirmation. Same principle NeuroPaca's own audit
trail used, rebuilt here standalone.

**Activation** — off by default. Turned on tier-by-tier once the pipeline has
run reliably for real, day-to-day use. Your call, exactly as already agreed.

## Voice output (TTS) — Step 7 — COMPLETED

Step 7 is fully implemented in `tts.py`, wired into `daemon.py` and
`main.py`, deployed live. Final engine choice, each one checked directly
against primary sources and benchmarked live before being trusted, same
discipline the rest of this doc uses:

- **English: Piper** (`en_IN-spicor`, NavGurukul AI Labs, hosted at
  `huggingface.co/navgurukul-ai-labs/text-to-speech-en-IN-piper`) — a
  dedicated Indian-English voice fine-tuned from Piper's own
  `en_US-ljspeech-medium` base for 1089 epochs on IISc's SPICOR English
  dataset. AGPL-3.0. 63.5MB, CPU-only (zero GPU/VRAM use — matters on this
  machine's shared 4GB card). Measured live: **~1.1s cold load, ~0.3s
  synthesis per sentence.**
- **Hindi: Kokoro** (`hf_alpha`, Apache-2.0, `github.com/hexgrad/Kokoro-82M`)
  — verified directly against Kokoro's own `VOICES.md`: four real Hindi
  voices exist (`hf_alpha`/`hf_beta` female, `hm_omega`/`hm_psi` male).
  Streamed via **RealtimeTTS** (KoljaB, MIT).
- Hinglish/code-switching stays explicitly OUT of scope — direct
  instruction, and independently one of the harder open problems in
  speech research (see the sources table's Hinglish citation).

**How this plugs into the existing architecture:** `tts.py` exposes
`speak(text: str, lang: str | None = None) -> None`, auto-detecting
Devanagari script for Hindi and defaulting to English otherwise (or an
explicit `lang=` override). Wired into `daemon.py`'s `_process_command`:
every resolved action gets a spoken confirmation alongside its desktop
notification, and an interim phrase ("Let me check.") covers anything
with real latency (a web search, an LLM-fallback answer) instead of going
silent for several seconds.

**Setup note — this is a real external dependency, not just pip
install:** Piper's voice model is a 63.5MB binary weight file, not
committed to the repo (see `.gitignore`). Download it before first run:

```bash
mkdir -p piper_voices
curl -sL -o piper_voices/en_IN-spicor.onnx \
  "https://huggingface.co/navgurukul-ai-labs/text-to-speech-en-IN-piper/resolve/main/en_IN-dataset=spicor-english-base=ljspeech-epochs=1089.onnx"
curl -sL -o piper_voices/en_IN-spicor.onnx.json \
  "https://huggingface.co/navgurukul-ai-labs/text-to-speech-en-IN-piper/resolve/main/en_IN-dataset=spicor-english-base=ljspeech-epochs=1089.onnx.json"
```

### What was tried and dropped along the way, and why

Three real engines were built, benchmarked, and removed before this
final shape — recorded here rather than silently deleted, same policy
every other resolved decision in this doc follows.

**MeloTTS (EN_INDIA)** was the original English voice — a real,
dedicated Indian-English speaker (MyShell.ai, MIT, verified straight from
its README), warm generation genuinely 0.15-0.20s/sentence. Its cold
start had a real bug: `device="auto"` resolved to CUDA on this machine,
and a separate BERT text-frontend lazy-loaded on the first *actual*
synthesis call (not at construction) cost 5.8-22.7s — caught live because
the daemon's "Ready" notification appeared instantly while its spoken
confirmation silently lagged up to ~20s behind it. Fixed properly (warm-up
now runs a real, silent synthesis call, not just constructs the model) —
but the deciding flaw wasn't the bug, it was that **the voice itself had
no alternate to switch to**: `EN_INDIA` was MeloTTS's only Indian-English
speaker, and its `tts_to_file()` signature exposes no pitch/timbre
control (`sdp_ratio`/`noise_scale`/`noise_scale_w`/`speed` only) — when it
didn't sound right, nothing could be tuned or swapped within the model.
Removed entirely once Piper's single voice turned out to actually sound
right, making MeloTTS's whole engine redundant, not just its default.

**Kokoro was also tried for English** (`af_heart`, then `am_michael` and
7 other real male voices after direct feedback) — genuinely good voice
variety (9 male + 9 female American voices, checked live), but zero
Indian accent, which was the actual point of this whole exploration.
Kept for Hindi only, where it already had real, verified voices.

**Punjabi (AI4Bharat's IndicF5, MIT)** was built and benchmarked — a real
gap-filler at the time, since no mainstream/Western TTS project has
Punjabi support. Voice cloning against a reference sample worked, but
latency did not: 14.4s (CUDA) / ~55s (CPU) per short phrase in isolation,
confirmed worse — 96s — in a later full-pipeline benchmark with every
other model also resident. Removed entirely rather than left in disabled;
`tts.py` no longer imports `transformers`/`torchaudio` for it, no
reference audio ships. If Punjabi output is wanted again, a cloud TTS
with real Punjabi support is the more promising direction, not a retry of
local voice cloning.

**Also considered, not chosen:** Google Cloud Chirp3-HD (30 real
Indian-English voices, verified against Google's voice-list docs) — a
real cloud fallback if a local voice ever stops being good enough, not
needed once Piper worked. Svara-TTS (Kenpath, 19 Indic languages incl.
Indian English, Apache-2.0) — genuinely well-matched in concept, but its
documented production deployment needs a 16GB+ VRAM GPU via Docker; this
machine's GPU has 4GB total, already shared with Kokoro. No verified
lightweight path existed. Chatterbox (Resemble AI, 65.3% preferred over
ElevenLabs in a cited blind study) and F5-TTS/Orpheus 3B — strong
voice-cloning models, no Punjabi, not evaluated further once Punjabi was
in scope. ElevenLabs — the cloud quality benchmark, same cost/privacy/
quota tradeoff as every cloud option in this doc.

**Humanizing pass (kept from the original design, still applies):**
`clean_for_speech` in `tts.py` strips markdown/code blocks/URLs/bullets,
truncates long responses to concise spoken summaries at sentence
boundaries, and both engines run at a 0.95 speed multiplier for a calmer,
less rushed cadence.

## Layer-2 cascade + conversational answers (2026-09-16) — DONE

**Problem this solves, stated plainly:** two real gaps, one root cause.
(1) `config.LLM_FALLBACK_ENABLED` had been off by default since it was
added — Gemini's free-tier quota (20 requests/day) turns "on" into "works
for a few requests, then silently stops," which isn't a real fallback.
(2) There was no conversational answering at all — "what is python" only
ever opened a search page; nothing actually read or spoke an answer. Both
trace back to the same thing: no local model was wired in as a safety net.

**What shipped:** `llm_intent.py` is now a real cascade, not a single
Gemini call:
1. **Gemini** (`config.INTENT_MODEL = "gemini-flash-lite-latest"`) tries
   first. One call does double duty — picks a tool if the utterance is a
   command, or (verified directly, not assumed) returns a plain text part
   instead of a function call when the utterance is a genuine question,
   which becomes a real spoken answer via the new `answer_question` skill.
2. On **any** Gemini failure — a 429, a deprecated/removed model (this
   project already hit that once with `gemini-2.5-flash`), a network
   error — caught broadly on purpose, not just the quota case — it falls
   through to a fully **local** model instead of giving up.
3. A 429 specifically also gets remembered via the new `quota_tracker.py`
   (`~/.local/share/voice-standalone/gemini_quota.json`, one field:
   exhausted-on-date) so the rest of that day's utterances skip straight
   to local instead of re-paying a network round trip to fail again. No
   guessed daily-request-count threshold — Google no longer publishes a
   flat RPD number (checked directly against `ai.google.dev`'s
   rate-limits page: it's account/tier-specific now, dashboard-only), so
   this reacts to the real 429 signal instead of a stale assumed number.

**Model choice — benchmarked, not assumed, same discipline as Step 7:**
- Checked the live model list for this API key directly: `flash-lite` is
  the smallest *text*-generation tier available ("Nano Banana" is
  image-generation despite the name; the Gemma models on this key are
  26B-31B, larger, not smaller — both easy things to assume wrong from
  names alone).
- Benchmarked `gemini-flash-lite-latest` against locally-available
  **Qwen2.5-1.5B-Instruct** and **Qwen2.5-3B-Instruct** (a GGUF already
  present on this machine from unrelated earlier work, served via Ollama —
  already running as a systemd service here) on the same 5 intent cases +
  3 open-ended questions:

  | | Gemini-flash-lite | Qwen2.5-1.5B | Qwen2.5-3B |
  |---|---|---|---|
  | Intent accuracy | 5/5 | 5/5 | 4/5 (hallucinated an action instead of "none") |
  | Intent latency (avg) | 800ms | 382ms (GPU) / 466-891ms (CPU) | 597ms (GPU) |
  | QA latency (avg) | 931ms | 1156ms (GPU) | 1636ms (GPU) |
  | QA phrasing | Most natural | Good, occasionally textbook-ish | Good, concise |

  **3B was dropped entirely** — slower and less accurate than 1.5B with no
  measured upside. **1.5B was chosen as the local fallback.**
- Ollama defaults to 100% GPU offload — checked directly with
  `nvidia-smi`/`ollama ps`: loading just the 1.5B model pushed VRAM usage
  to ~2GB, real contention risk against the same 4GB card Kokoro already
  uses. `local_llm.py` forces CPU-only (`num_gpu: 0`) for this
  reason — still 400ms-2.5s per call, easily fast enough for a path that
  only runs after Layer 0, Layer 1, *and* Gemini have all already failed.
- The local classify+answer combo was tested as one JSON-constrained call
  first, and found unreliable: the model correctly recognized "why is the
  sky blue" needed an answer but then didn't write one inside the same
  constrained response. Split into two separate calls (classify, then —
  only if classified as a question — a second unconstrained call to
  actually answer), each tested working on its own, which is what shipped.
  Known, accepted tradeoff: local 7-way classification (5 tools +
  answer_question + none) measured 4/5 in a quick check, lower than
  Gemini's — acceptable since Gemini is still the primary path every time
  quota allows it; local is the fallback-of-a-fallback, not the default.

**New/changed files:** `local_llm.py` (Ollama client for Qwen2.5-1.5B),
`quota_tracker.py` (the 429-remembering state file), `llm_intent.py`
(rewritten as the cascade, same `resolve_intent(text) -> (name, args)`
contract as before — zero changes needed in `daemon.py`/`main.py`), a new
`answer_question` skill in `actions.py` (SAFE tier, default) that speaks
the generated answer **and** still opens a Google search page for the
same question — both, not one instead of the other, per the original ask.

**Real external dependency now, not just a pip package:** this needs
Ollama installed and running (`systemctl is-active ollama`) with
`qwen2.5:1.5b-instruct-q4_K_M` pulled. Not managed by `requirements.txt`
since it isn't a Python package — worth knowing before deploying this
elsewhere, it won't "just work" from a fresh `pip install -r
requirements.txt` alone.

**`LLM_FALLBACK_ENABLED` turned ON (2026-09-16), by direct instruction** —
not a default that quietly changed itself; the quota dead-end that
justified leaving it off is fixed, and flipping it was an explicit call.

**Wikipedia fast-path added the same day (`wiki_fastpath.py`), in front of
the LLM cascade:** for narrow "what is X"/"who is X"/"what's X"/"tell me
about X" phrasing, tries Wikipedia's public API first — free, no API key,
no quota — before ever reaching Gemini/Qwen. Two HTTP calls, not one,
because that was verified directly to matter: fetching the summary for
the literal spoken topic isn't reliable for common single-word topics —
"python" (the literal extraction from "what is python") resolves to a
disambiguation page ("Python may refer to..."), not the programming-
language article. Wikipedia's own search API's top hit for "python", by
contrast, correctly is "Python (programming language)" — so this searches
first, then fetches the summary for whatever real title that search
returns. Deliberately scoped narrow (not causal/explanatory phrasing like
"why is the sky blue" — those aren't well served by a raw wiki summary,
and stayed on the LLM cascade) and always tried regardless of
`LLM_FALLBACK_ENABLED`, since it isn't an LLM call at all — on any
uncertainty (no phrasing match, no search hit, disambiguation, network
failure) it returns nothing and falls straight through to the LLM cascade
exactly as before this existed. Wired into both `daemon.py` and `main.py`
between Layer 1 and the LLM cascade. On a hit, opens the actual Wikipedia
article (not a Google search) alongside the spoken answer — `actions.py`'s
`answer_question` now takes an optional `url` for this.

## Daemon startup hang — process isolation + safe spawning (2026-09-16) — FIXED

**English switched to Piper** (`en_IN-spicor`, real Indian-English voice
with more variety concerns than MeloTTS could offer, direct feedback) —
replacing it surfaced a genuine, serious infrastructure bug the previous
engine never happened to trigger: the daemon (both as a direct process
and as the real systemd service) hung intermittently and
non-deterministically during startup — sometimes reaching "Ready" in
seconds, sometimes stuck 14+ real minutes at 0% CPU, sometimes 60s+
burning real CPU with no error. Reproduced repeatedly against the real,
unmodified entry point; simplified reproductions misleadingly kept
succeeding (a real trap — several fix attempts looked confirmed-working
until re-tested against the actual `daemon.py`/systemd service).

**Two distinct, real root causes, both found and fixed, not guessed:**

1. **torch bundles its own private `libgomp.so.1`**, separate from the
   system's. With onnxruntime (openwakeword) and torch (Kokoro) both
   loaded in one process, constructing Kokoro's engine reliably hung —
   isolated by timing each startup stage separately until the exact
   failure point (a torch `nn.LSTM` layer construction) was pinpointed.
   Fixed in the new `_process_guard.py`: preloads one canonical libgomp
   via `LD_PRELOAD` before either library can load its own copy, by
   re-execing the process once at the very top of `daemon.py`/`main.py`
   (`execve` keeps the same PID, safe under systemd's process tracking —
   `LD_PRELOAD` can only be set before a process starts, not from
   already-running Python code).
2. **Python's `subprocess` module creates children via `fork()+exec()`
   on POSIX.** `fork()` in a multi-threaded process is a well-documented
   hazard: it only duplicates the calling thread, so if another thread
   (onnxruntime's or numpy's own internal thread pools) held a lock at
   that exact instant, the child inherits it permanently locked — the
   thread that would release it doesn't exist in the child. This hit
   **both** `tts.py`'s own subprocess spawns **and** `daemon.py`'s plain
   `subprocess.run(["notify-send", ...])` call — the latter was the
   real, previously-camouflaged cause of the daemon hanging on its own
   *first* "Ready" notification, found only after the TTS-specific
   fixes were already in place and the real service kept hanging anyway.

**The actual fix, verified against the real service, not assumed:**
`tts.py` now runs **all synthesis in an isolated subprocess** (itself,
invoked as a script via `python3 tts.py --speak-worker <lang> <text>` /
`--warm-worker`) rather than in `daemon.py`'s/`main.py`'s own process at
all — architectural isolation instead of chasing every library pairing
one at a time. Both that spawn and `_process_guard.py`'s new
`safe_run()` (used for `notify-send`) go through `os.posix_spawn`
(never forks) **with a hard timeout** (25s for TTS, 10s for
`notify-send`), so neither can block the daemon forever even if some
as-yet-unidentified cause resurfaces — a stuck call becomes "this one
thing didn't happen" (caught and logged by both call sites), not "the
daemon never reaches Ready."

**Real cost of this fix, stated honestly:** each `speak()` call now pays
its own process-spawn + cold-load time (Piper ~1.1s, Kokoro a few
seconds) rather than reusing a warm in-process model — the "Let me
check" interim phrase already exists specifically to cover exactly this
kind of multi-second latency, and a daemon that reliably reaches "Ready"
matters more than shaving a second off each spoken reply.

**Verification:** 3 consecutive clean restarts of the real systemd
service, each reaching "Ready" and speaking it aloud —
`systemctl --user restart voice-daemon` three times in a row, watched
each one settle from multiple subprocesses back to a single healthy
main process. `smoke_test_skills.py`: 176/176.

**Known, accepted scope limit, not silently ignored:** `actions.py`'s
many other `subprocess.run` calls (volume, brightness, opening apps,
etc.) were not converted to the same safe-spawn pattern. They run one at
a time, well after startup, with real idle time between them — a much
lower-risk window than the tight startup sequence this fix needed to
guarantee. A real residual risk, not zero, but converting all of
`actions.py` was out of scope for what this fix needed to guarantee
(the daemon reliably starting) given the time already spent isolating
the two root causes above.

## Unified boot-to-shutdown setup + 7-day soak monitor (2026-09-16) — DONE

**One script installs and enables the whole system together**:
`setup_services.sh` (repo root) installs `voice-daemon.service`,
`voice-tray.service`, and the new `voice-soak.service`, all as
systemd `--user` units tied to `graphical-session.target` — that target
is what makes "starts with turning on the system, closes safely on
shutdown" actually true: it starts once the login session's compositor/
D-Bus/audio are up (these services need real audio devices and a
notification server, not just the OS booting), and systemd sends each
one a normal `SIGTERM` when that session ends, logout or real shutdown
alike. Not a custom shutdown hook — systemd's own session lifecycle, the
same mechanism the daemon and tray already relied on before this.

**`soak_monitor.py`** (new) samples both services every 5 minutes for 7
days — `ActiveState`, restart count, memory, plus a count of real
commands processed in that interval (from `skills/_audit.py`'s existing
log) — and writes one JSON line per sample to
`~/.local/share/voice-standalone/soak_log.jsonl`. On completion, whether
that's the full 7 days elapsing or an early `SIGTERM` (verified live:
both paths tested directly, not assumed), it writes a summary
(`soak_summary.json`) with uptime %, restart deltas, and memory min/max/
avg per service.

**Pauses on shutdown, resumes on restart — not wall-clock since first
launch** (fixed 2026-09-16, direct user feedback on the original
version: a reboot mid-soak either reset the count to zero or would have
counted downtime as observed uptime, neither of which is what "7-day
soak" means). `soak_state.json` persists real observed time only
(`cumulative_elapsed_seconds`) across runs; on `SIGTERM` the current
session folds into that total before exit, so the next login resumes
the 7-day count from exactly where it left off, not from zero. A
one-time migration bootstraps a soak that was already running under the
old version from its last sample's `elapsed_hours` instead of
discarding already-collected data — also doubles as a self-healing
fallback if the process ever exits ungracefully (SIGKILL, OOM, power
loss) before writing state. Verified with a full pause/resume cycle
before applying to the real running soak.

Deliberately **not** the main NeuroPaca project's
existing soak infrastructure (`scripts/soak_probe.py` etc.) — that reads
a daemon-authored `health_check()` JSON dump this project's daemon
doesn't have; this reads what actually exists here instead
(`systemctl --user show` + the audit log), same "read the real source of
truth, don't build one this ask didn't call for" reasoning.

**`voice-soak.service` has no `Restart=` directive, on purpose**: the
script exits `0` on its own once the 7 days elapse or it's told to
stop — an automatic restart on a *successful* exit would silently start
a brand new 7-day run forever, the opposite of what a bounded soak test
means. A real crash (non-zero exit) surfaces as a stopped, failed unit
(`systemctl --user status voice-soak`), not something quietly retried.

**Re-running `setup_services.sh` is safe** — it won't restart an
already-running daemon/tray (systemd's own `enable --now` no-ops if
they're already up) and deliberately does **not** auto-restart
`voice-soak` on a re-run, so picking up a code change doesn't silently
reset an in-progress 7-day count back to day 0. `./setup_services.sh
--uninstall` stops, disables, and removes all three units (leaves the
soak/audit log data files alone — those are results, not install
artifacts).

## Explicitly deferred / not part of this doc

- Safety tier *activation* — Step 6 built the mechanism (tiers, confirm-loop,
  machine-scan, audit log); `config.SAFETY_TIERS_ENABLED` stays off by
  default. Turning it on, tier by tier or all at once, is still entirely
  your call, exactly as always specified.
- Editable preview from the tray specifically — `daemon.py`'s DANGEROUS
  confirm is confirm-or-cancel-by-click only, not text editing (no text
  input exists on a tray icon). Full editing lives in `main.py`'s terminal
  mode today. A real fix would need a GTK dialog or similar wired to the
  tray — not attempted here, and worth flagging rather than pretending the
  tray path is equivalent.
- Local-only model swap (replacing Gemini with something fully on-device, like
  your friend's build) — a separate decision, not assumed here either way.
- Any NeuroPaca integration — out of scope by design until this proves itself
  standalone.
- Categories I (fun/personality) and K (network/connectivity) — skipped
  entirely per your explicit direction during Step 4, not built, not
  scheduled.
- The full 16-item Category G catalog (alarms, timers, notes, clipboard,
  meal list) — only a 2-skill test to-do was built; the rest needs its own
  persistence/scheduling design, deliberately out of scope for that pass.
- Every ⚙️-tagged skill across every category (Spotify, calendar, email,
  Wolfram Alpha, weather, news, Telegram, VPN, etc.) — no external accounts
  were set up during this build; see open question 4 below.
- Extending Layer 1 (semantic match) to the ~55 skills added in Step 4 — they
  resolve via Layer 0 grammar only; paraphrases that miss their regex fall
  straight to the LLM instead of a fast local match, unlike the original 50.
- Hinglish/code-switched speech — explicitly ruled out by direct instruction
  when Step 7 was scoped, not an oversight. Independently one of the harder
  open problems in speech research generally (see the sources table).
- Multi-language INPUT matching (Layer 0/1 understanding spoken Hindi/
  Punjabi commands, not just producing spoken Hindi/Punjabi output) — Step
  7 is output-only; this is a separate, larger undertaking that would mean
  parallel regex/embedding coverage for every skill, multiple languages
  over, not attempted here.

## Open questions

1. ~~Exact Layer 1 thresholds~~ — RESOLVED (Step 2): 0.75/0.55, tuned against
   58 real independent test cases (`smoke_test_semantic.py`). Correct-match
   and reject-worthy scores overlap in a 0.71-0.75 band for this model, so no
   threshold gets everything right — chose zero false-accepts over zero
   false-rejects. Revisit if the skill catalog grows enough to shift this band.
2. ~~Which local embedding model to use for Layer 1~~ — RESOLVED (Step 2):
   `fastembed` + `BAAI/bge-small-en-v1.5`, benchmarked live against
   `sentence-transformers`/all-MiniLM (see Layer 1's "Honest cost" note above).
3. ~~Where does the "known list" come from~~ — PARTIALLY RESOLVED (Step 4):
   apps, C2 sites, and common named folders (downloads/documents/desktop/
   pictures/music/videos/home, via `skills/_paths.py`) are solved. Bookmarks
   and contacts remain genuinely open — no skill needs them yet, so this is
   moot until one does.
4. Which ⚙️-tagged skills are worth the account/API setup at all versus cut —
   e.g. is Telegram integration something you'll actually use, or dead weight
   carried over from Mycroft/OVOS precedent that doesn't fit you? Still fully
   open — no ⚙️ accounts were set up during Steps 1-6.
5. (New, from Step 6) Exact tier assignments for anything beyond the 11
   already-reasoned 🔒 skills plus the 2 added REVIEW items are a judgment
   call, not exhaustively re-audited across all 105 skills — worth a second
   look before ever setting `SAFETY_TIERS_ENABLED=true` for real.
6. ~~IndicF5's real-world latency~~ / ~~Punjabi voice quality~~ / ~~Fixed
   vs. auto-detected output language~~ / ~~MeloTTS `EN_INDIA` latency &
   default viability~~ — all RESOLVED and now moot: MeloTTS and IndicF5
   were both benchmarked, then removed; Kokoro's English path was also
   tried and dropped. Full history (numbers, the MeloTTS cold-start bug
   and its proper fix, why each engine got replaced) lives in Step 7's
   "What was tried and dropped along the way, and why" — not repeated
   twice in one document. Language detection is RESOLVED and current:
   `tts.detect_lang` routes Devanagari to Hindi, Latin to English
   (Piper), explicit overrides also accepted.

## Build steps

Not a one-shot build — each step should be working and tested before the next
one starts, so a bad threshold or a slow model only ever touches the one step
that introduced it.

**Step 1 — Layer 0 only, categories A + B + C1's unmarked items (48 skills, zero external accounts)**
(A=24 + B=10 + C1-unmarked=14; C1's 8 ⚙️-tagged items — Wolfram Alpha, weather,
forecast, news, movie info, currency conversion, stock price, translate — need
external APIs and move to Step 4 instead, to keep this step truly account-free.)
- Turn today's flat `skills.py` into the `skills/` package from Module
  boundaries above, one file per category.
- Write regex/keyword matchers for all of A, B, C1 — reuse the correction-layer
  pattern already proven on `open_app` for anything naming a real thing.
- `llm_intent.py` stays exactly as the catch-all for everything else, unchanged.
- Manually test each skill with a few phrasings, including deliberately odd
  ones, before moving on — this is where the app-name bug got caught last time.

**Step 2 — Benchmark, then add Layer 1 (semantic match)** — DONE
- Benchmarked `sentence-transformers`/all-MiniLM vs `fastembed`/bge-small live
  on this machine before building anything on top — the torch-based option's
  ~15s warm startup cost was a real, previously-unknown finding; fastembed's
  ~1.3-1.9s won with equivalent per-utterance latency. Answers open question #2.
- Wrote 3-5 paraphrased example phrases for all 50 skills from Step 1 (the
  48-skill catalog maps to 50 registered matcher/executor functions) in
  `skills/semantic_match.py`, plus a shared set of generic argument extractors
  (percent, on/off state, app-name-via-resolver, trailing query/word/location/
  expression) since Layer 0's regex-coupled extraction doesn't carry over.
- Precomputed and cached example embeddings at startup (warmed up eagerly in
  `main.py` before the input loop, not lazily on first miss — a lazy load
  would otherwise surface as a surprise ~1.9s hang mid-conversation).
- Built `smoke_test_semantic.py`: 46 independently-worded test cases (neither
  Layer 0's exact trigger phrasing nor semantic_match's own reference
  examples — testing either would be circular). Tuned thresholds against real
  measured scores, not guesses: correct matches and reject-worthy negatives
  turned out to *overlap* in the 0.71-0.75 range for this model, so no single
  threshold gets every case right — moved the threshold from 0.82/0.60 to
  0.75/0.55, prioritizing zero false-accepts (the dangerous failure — a wrong
  action silently executing) over false-rejects (safe — just falls through to
  the LLM). Also found and fixed a real blend-formula bug: the original
  "0.7×cosine + 0.3×token-overlap" penalized genuine paraphrases (which by
  definition share few words with their reference example), the opposite of
  its intent — changed to `max(cosine, blended)` so overlap can only help.
  Final measured result: **50/58 test cases correct, 0 false accepts**, 7
  safe false-rejects (fall through to LLM), 1 low-harm wrong-skill collision
  (get_time vs. timezone_conversion — a fine-grained distinction even a
  general-purpose embedding model struggles with) left as a known, documented
  limitation rather than chased indefinitely. Answers open question #1.
- Also caught and fixed a real extractor bug while diagnosing a false reject:
  `switch_workspace` scored 0.93 (well above threshold) but silently failed
  because the number-extractor only understood cardinal words ("two"), not
  ordinals ("second") — fixed the word-number map.

**Step 3 — Generalize the correction layer** — DONE
- Built `skills/_correction.py`: `resolve_against_known()` implements the
  documented ensemble exactly (substring match first, else 0.6×edit-distance-
  ratio + 0.4×token-overlap, cutoff-gated, else None) as reusable, domain-
  agnostic infrastructure — ready for Step 4's future site/file skills.
- `_app_resolver.py` refactored into a thin wrapper supplying the installed-
  apps candidate list; it previously used a plain `difflib.get_close_matches`
  call (only the edit-distance-style signal, no token-overlap blend) — now
  genuinely uses the documented ensemble.
- Audited every argument across the 50 built skills for whether it names a
  "real thing" worth correcting: only `app_name` qualifies right now (an
  authoritative local list exists, and a wrong guess launches the wrong app).
  `timezone_conversion`'s `location` was considered and deliberately
  excluded — its executor already delegates to time.is's own lenient
  place-name resolution; forcing our own stricter local correction against a
  necessarily-incomplete list would reject valid inputs, a regression, not an
  improvement. Documented all exclusions inline in `_correction.py` rather
  than leaving them looking like an oversight.
- **Live-testing bug found and fixed:** the refactor initially checked
  substring containment in BOTH directions (spoken-in-name OR name-in-spoken).
  The original, correct behavior only checked one direction. Checking both
  broke a real case: "cosmic files" matched `org.gnome.Nautilus` (whose
  display name is just "Files" — a coincidental substring of the longer,
  more specific spoken phrase) instead of the intended `com.system76.
  CosmicFiles`, because the substring pass returns on the first hit, not the
  best-scoring one, and never even reached the ensemble scoring that ranked
  CosmicFiles correctly at the top. Fixed to one-directional (spoken must be
  contained in the candidate's full name — matches how people actually
  abbreviate names, e.g. "code" for "Visual Studio Code," never the reverse).
  Verified against all 6 installed COSMIC-prefixed apps post-fix.

**Post-Step-3 bug hunt (code-review pass over Steps 2+3):**
- `_trailing_location`/`_trailing_word`/`_app_name_trailing`'s cue-word lists
  were missing common filler ("the," "'s," "tell," "could," "you," etc.),
  leaving garbled multi-word junk glued to the extracted value instead of
  just the target word/city — e.g. "Paris" was extracting as "'s the local
  Paris." `_APP_CUE_WORDS` additionally stripped "editor"/"manager"/"client,"
  which are literal parts of real app names, not filler — fixed all three
  lists and extracted the shared strip/collapse/trim logic into one
  `_strip_cue_words()` helper instead of five copies of the same three lines.
- `_percent_arg` took the first 1-3 digit number anywhere in the sentence,
  so "it was at 20 earlier, set it to 60 percent" silently set volume to 20
  instead of 60 — a wrong action executing, not a safe fallback. Fixed to
  prefer a number explicitly tagged "%"/"percent," else the last bare number
  (natural speech states context before the actual instruction).
- `main.py`'s eager Layer 1 warm-up had no error handling, unlike every
  other fallible call in the file — a model-load failure (no network on
  first-ever download, a corrupted cache) would crash the assistant before
  it ever started listening. Fixed with the same try/except pattern as
  everything else, degrading to Layer 0 + LLM-only for that session instead.
- **Found a second, deeper instance of the Step 3 substring-ordering bug**
  while verifying the app-name fix with a real installed app: "settings"
  matched `com.system76.CosmicSettings.LegacyApplications` (whose 48-
  character desktop_id happens to contain the word "settings") instead of
  the exact match `org.gnome.Settings`, because `resolve_against_known()`
  still returned on the first substring hit found in list order. The
  one-directional fix from Step 3 was necessary but not sufficient — the
  real fix was structural: collect ALL substring-containment hits, then
  pick the one with the shortest match_key (the tightest, most specific
  match — an exact match being the tightest possible) instead of whichever
  the candidate list happened to enumerate first. Verified this doesn't
  regress the original short-abbreviation case ("brave" -> "brave-browser")
  that an earlier length-ratio-scoring attempt at this same fix broke.
- Found independently (not from the review): `actions.calculator()`'s
  character-strip regex silently deleted word-form operators as junk —
  "multiply 25 by 16" stripped to "2516" (both numbers concatenated with no
  operator) before falling back to Google, instead of computing 400. Fixed
  by translating "divided by/multiplied by/times/plus/minus" and the
  verb-first "multiply X by Y"/"divide X by Y" forms to symbols first.
- All fixes reverified against the full regression suite (176/176 Layer 0,
  50/58 Layer 1, all 6 COSMIC apps, plus the specific adversarial phrases
  each bug was found with) before considering Step 3 closed.

**Step 4 — Scale toward the full 181, wave by wave** — CLOSED (by explicit
scope decision, not full 181-skill completion)
- Built: D (media), E (files), F (dev tools), a minimal G (test to-do only,
  not the full 16-item catalog), J (assistant meta), L (maintenance).
- Explicitly skipped per your call: I (fun/personality) and K (network/
  connectivity) — not built, not planned for this pass.
- ⚙️-tagged skills across every category (Spotify, calendar/email, Wolfram
  Alpha/weather/news/etc.) remain deferred — no accounts were set up this
  pass, consistent with "only add once the account exists."
- Layer 1 (semantic match) was NOT extended to cover any of Wave 1/Wave 2's
  ~55 new Layer-0 skills — they exist only in the deterministic grammar
  layer for now. This is a known, real gap: paraphrases of these new skills
  that don't match their Layer 0 regex will fall through to the LLM instead
  of resolving locally, unlike the original 50 which have both layers. Worth
  a dedicated future pass, not silently assumed to already be covered.

**Wave 1 (E + F) — DONE.** 16 Files skills + 13 new dev-tool skills (F02-F14;
`run_terminal` already existed from Phase 0). `skills/_paths.py` added as
shared spoken-folder-name resolution (downloads/documents/desktop/etc. → real
XDG paths) plus a common-directories file finder, used by both categories.

Placed E and F *before* C1 in `skills/__init__.py`'s scan order — verified
empirically, not assumed, that C1's broad `google_search` fallback ("search
for .+") would otherwise steal E's "search my files for X" and similar
dev-tool phrasing before it ever reached the more specific skill.

Notable design/safety decisions made while building:
- `delete_file`/`empty_trash` use `gio trash`, not a raw unlink — moves to
  the recoverable XDG trash even ahead of the real Step 6 confirm-loop, a
  deliberately safer default for a 🔒-tagged skill that's live before tiers
  exist.
- `kill_process` (F04) uses `pkill -f`, unlike B08's `force_quit` — this one
  is meant for scripts/daemons a developer names directly (e.g. "kill
  pytest"), where matching the full command line is the expected, useful
  behavior, not a risk to guard against the way B08's GUI-app case was.
- `install_updates`/`restart_service` (🔒) use `pkexec`, never raw `sudo` —
  pkexec always requires an interactive OS-level graphical password dialog,
  so a voice command can never silently escalate privileges even before
  Step 6's real tier system exists. Raw `sudo` would either hang the voice
  loop on a nonexistent terminal prompt, or — worse — execute silently if
  passwordless sudo happened to be configured.
- `check_cpu` computes a real instantaneous percentage from two `/proc/stat`
  samples 200ms apart (the standard technique) rather than reporting the
  cumulative-since-boot total a single sample would give.
- Live-tested every non-destructive executor for real (git status, cpu/
  memory/load, package version, ping, process list/kill against a throwaway
  process, and the full file lifecycle: create → rename → copy → compress →
  extract → delete-to-trash, using disposable test artifacts, cleaned up
  after). Did NOT live-test `install_updates`/`restart_service` (would
  actually modify the system or restart a real service) or `empty_trash`
  (irreversible, could delete things already in the user's trash) —
  implemented and documented, not blindly assumed correct.
- Found and fixed a genuine self-inflicted testing artifact along the way:
  an inline `python -c "..."` test script that itself contained the literal
  string "sleep 300" got killed by its own `pkill -f` call, since `-f`
  matches the full command line — including the test harness's own source
  text. Confirmed this can't happen in real usage (the actual assistant
  process's command line is just `python3 main.py`, never containing
  transcribed text) by rerunning as a proper script file instead.
- One outdated test assumption found and fixed: two smoke-test cases from
  Step 1 asserted "restart the nginx/apache service" should match nothing,
  which was correct *before* `restart_service` existed — updated to expect
  the new skill, not reverted.
- One functional limitation found via live testing, documented rather than
  silently left: `find_file()` resolves same-named files across multiple
  folders by fixed search-directory priority (Downloads before Documents,
  etc.) with no way to disambiguate when the same filename exists in more
  than one — surfaced when testing `move_file`, which correctly refused to
  overwrite rather than doing anything destructive, but the underlying
  ambiguity remains a real, worth-knowing constraint.

**Wave 2 (D + minimal G + J + L) — DONE. Step 4 closed; I and K explicitly
skipped per your call.** 13 media skills, 2 test to-do skills (not the full
16-item G catalog — no alarm/timer/note/clipboard persistence design was in
scope for this pass), 5 assistant-meta skills, and 6 maintenance skills.
`skills/_session_state.py` added — minimal in-memory state (asleep flag,
last response) shared between `main.py` and the sleep/wake/repeat skills;
`main.py` now gates *execution* (not matching) on the asleep flag, and
wraps dispatch in a stdout-capturing buffer so "repeat that" works without
rewriting every existing executor to return a string instead of printing.

Notable design decisions:
- `change_wallpaper` writes COSMIC's `CosmicBackground` config directly (same
  approach as `toggle_dark_mode`), and — same finding as `toggle_dnd` in the
  earlier bug hunt — does NOT send a reload signal: checked `cosmic-bg`'s
  `/proc/<pid>/status` and it does not catch SIGHUP either, so signaling it
  would kill it, not reload it. Documented as "saved; may need a session
  restart to visibly apply," not silently assumed to work live.
- `play_youtube_video` and `set_media_volume` were catalog entries this pass
  did NOT build as separate skills — found via live regression testing that
  both would be genuine duplicates: C1's `youtube_search` already lists
  "play" as a trigger verb for "play X on YouTube," and A05's `set_volume`
  pattern already matches "volume ... NUMBER" regardless of the word "media"
  appearing first (and category A is scanned before D). Removed the
  redundant code rather than leave two skills doing the identical thing.
- Found and fixed a real matcher regression while testing: A23's
  `battery_status` pattern included "health" as a trigger word from Step 1,
  written before `battery_health` (L01, capacity/wear via `upower` — a
  genuinely different thing from charge %) existed. Removed "health" from
  A23's pattern so it correctly routes to L01 now.
- Live-tested every executor that was safe to run for real, including
  `take_photo` (a real webcam capture — verified the output was a genuine
  1280x720 JPEG, not just "didn't crash"), `change_wallpaper` (both by name
  and the random-pick path, restoring the original config after), and
  `open_camera_app`/`browse_local_media` — all cleaned up after. Did not
  live-test `clear_app_cache` (would delete real cache directories a running
  app might need).

**Tray + always-on daemon (built ahead of Step 5, at your request)** — DONE.
Replaces `main.py`'s `input()`-gated push-to-talk as the real, user-facing
way to run this day to day — `main.py` itself is kept only for manual/dev
testing, per the standing "no terminal control surfaces" preference (see
memory: NeuroPaca's own tray had an equivalent text/CLI control surface
removed for the same reason).

- `daemon.py` — the always-on loop (your `.venv`, since it needs Gemini/
  fastembed/etc.). Click 1 (tray) starts recording, click 2 stops,
  transcribes, resolves, executes — feedback via desktop notifications
  (`notify-send`), since there's no terminal to read once this runs as a
  background service.
- `tray.py` — the tray icon, deliberately under SYSTEM python3, not the
  project venv, matching NeuroPaca's own `scripts/neuropaca_tray.py`
  precedent exactly: PyGObject/AyatanaAppIndicator3 are desktop-shell
  bindings, not project runtime dependencies. Two independent processes,
  not one process mixing GTK's main loop with the venv's audio/LLM
  pipeline, talking over a simple FIFO (`~/.local/share/voice-standalone/
  toggle.fifo`) — the tray writes a toggle in a background thread (so a
  click never blocks the whole GTK UI if the daemon is mid-command), the
  daemon reads it in a loop.
- `audio_capture.py`'s recording core was split into `record_until_stopped
  (path, stop_event)` — decoupled from *how* stop is signaled — with
  `record_until_enter` now a thin wrapper over it for `main.py`'s dev-only
  terminal mode.
- Both installed as `systemd --user` services (`systemd/voice-daemon.service`,
  `systemd/voice-tray.service`), enabled against `graphical-session.target`
  (needs the compositor's session — Wayland display, D-Bus, audio — already
  up, same reasoning as NeuroPaca's units) — they now start automatically
  every login, no manual launch needed.
- **Real platform constraint found via live testing, not assumed:**
  AppIndicator/StatusNotifierItem is fundamentally menu-based on most
  desktops — a bare click with no menu attached often does nothing. Built
  as click → one-item menu ("Start Recording"/"Stop Recording," label
  reflects state) → click that item, the closest reliable approximation of
  a toggle this protocol actually supports, rather than claiming true
  single-click and having it silently not work.
- **Found via live testing:** stdout is fully block-buffered once not
  attached to a terminal (both `nohup` and `systemd` trigger this) — a
  background service's own startup prints would sit invisible in a buffer
  indefinitely. Fixed with `PYTHONUNBUFFERED=1` in the daemon's service unit.
- Verified end-to-end for real: started both services live, monitored
  D-Bus directly for the actual `org.freedesktop.Notifications.Notify`
  calls (not just "did the process not crash") across two full toggle
  cycles on the real, permanently-enabled service instance.
- Visual/click verification (does the icon actually render, does clicking
  it feel right) needs the user's own eyes and mouse — not something
  verifiable by running commands.

This changes where Step 5's "Capture module" work below actually belongs:
wake-word detection is now `daemon.py`'s concern, not `main.py`'s — the
FIFO-triggered start already replaces one half of what a wake-word would
trigger (starting capture); only the "detect the hotword" half is new.

**Step 5 — Wake word + turn detection** — DONE
- Hotword: `openwakeword`'s bundled `hey_jarvis` model (same wake word the
  old, since-removed A6.3 implementation used) — verified all model/VAD
  files ship inside the pip package itself, no separate download step.
- Turn detection: openwakeword's bundled Silero VAD wrapper (`openwakeword.
  vad.VAD`), not a separate LiveKit model — same *spirit* the doc asks for
  (reads whether you're still talking, not a flat timer), a genuinely
  different, simpler mechanism than LiveKit's own semantic turn-detector.
  Documented as that substitution, not silently passed off as the same thing.
- `daemon.py` rewritten around one continuous `sd.InputStream` (not
  opened/closed per command like `main.py`'s simpler push-to-talk) feeding
  a small idle/recording state machine. Manual (tray FIFO) and wake-word
  triggers are fully independent — manual sessions ignore VAD entirely and
  stop on the second click, exactly as precise as before; wake-word
  sessions stop automatically after ~1.2s of continuous non-speech
  following detected speech, or a 4s timeout if nothing is ever said, or a
  20s safety cap regardless.
- Asleep (`session_state`) disables WAKE-WORD detection specifically —
  `sleep_stop_listening` is named "stop *listening*" for a reason. The
  tray's manual toggle keeps working regardless of asleep state; it's the
  only way to say "wake up" while wake-word listening is paused.
- Verified live: 12+ seconds of real ambient audio produced a max wake-word
  score of 0.0 and a max VAD score of 0.27 (well below both activation
  thresholds) — no false positives on room noise/silence. Full manual
  toggle cycle re-verified end-to-end via D-Bus after the rewrite. Did NOT
  verify true-positive wake-word detection live — that needs an actual
  human saying "hey jarvis," which isn't something a command can do; that
  part is yours to confirm.
- **Found and fixed a real, unrelated, critical bug while testing:**
  `gemini-2.5-flash` (this project's STT/intent model since Step 0) returned
  a live 404 — "no longer available to new users." The API's own error
  explicitly recommended `gemini-3.6-flash`, confirmed present in this key's
  live model list with full `generateContent` support; switched both
  `STT_MODEL` and `INTENT_MODEL` to it in `config.py`. This was blocking
  every single voice interaction, wake-word-related or not.
- Added `openwakeword` to `requirements.txt`; installed and then removed
  `webrtcvad` (evaluated as a VAD option, turned out unneeded once
  openwakeword's own bundled VAD was confirmed to accept the same chunk
  size the wake-word model already uses).

**Step 6 — Safety tiers** — DONE (mechanism built; activation stays off by
default, exactly as the doc always specified — this closes the mechanism,
not the "only once daily-used and stable" activation decision, which
remains entirely your call)

- `skills/_tiers.py` — the static tier registry. DANGEROUS is exactly the 11
  skills already carrying an individually-reasoned 🔒 comment from Steps
  1-4 (`shutdown`, `restart`, `force_quit`, `rename_file`, `move_file`,
  `delete_file`, `empty_trash`, `kill_process`, `install_updates`,
  `restart_service`, `run_terminal`) — this registry collects those
  existing, specific judgment calls rather than re-deriving them. Two
  REVIEW additions, each independently justified: `clear_app_cache`
  (deletes real data, though regenerable) and `extract_archive` (can
  silently overwrite files at the destination). Everything else — the
  large majority — is SAFE by default.
- `skills/_machine_scan.py` — the pattern tripwire (`rm -rf`, `dd`, `mkfs`,
  a raw write to `/dev/sd*`, piping a download into a shell, `sudo`/
  `pkexec`, `kill -9`, a fork-bomb pattern, recursive `chmod 777`).
- `skills/_audit.py` — always-on, regardless of `SAFETY_TIERS_ENABLED`.
  Same principle Step 1's correction layer established: this is
  observability, not a gate, so it doesn't wait on the activation
  decision. One JSON line per resolved action (heard text, skill, args,
  tier, outcome) to `~/.local/share/voice-standalone/audit.jsonl`.
- `confirm_loop.py` — the generator: pauses with a preview + machine-scan
  warnings before running anything DANGEROUS, resumes with either the
  original args, an edited value, or a cancel. `main.py` (terminal mode)
  gets the full spec — a real editable preview via `input()`. `daemon.py`
  (the tray path) gets a documented, deliberately lighter variant: no text
  editing is possible from a tray icon, so it notifies with the preview
  and warnings and waits up to 10 seconds for a second tray click as
  confirm, auto-cancelling on timeout. Editing a misheard filename from
  the tray specifically isn't built yet — that's a real gap, not an
  oversight; `main.py`'s terminal mode is where full editing lives today.
- `config.SAFETY_TIERS_ENABLED` — the activation switch, off by default
  (`SAFETY_TIERS_ENABLED=true` env var to turn it on). Flipping it is the
  only thing that changes DANGEROUS/REVIEW behavior; the audit log runs
  either way.
- **Found and fixed a real bug while testing the confirm-loop itself:**
  `run_terminal` used `subprocess.run(command, shell=True)` with no output
  capture, so the child process inherited the parent's real stdout file
  descriptor directly — invisible to `contextlib.redirect_stdout`, which
  only intercepts Python-level `sys.stdout` writes. This silently broke
  two things at once: the confirm-loop's own "real stdout/stderr feeds
  back into the next reasoning step" requirement (it fed back nothing for
  the one skill this mechanism matters most for), and `repeat_last_response`
  (J07) for any terminal command, quietly, since Step 4 — an existing
  feature that had never actually worked for this specific skill until
  this fix added `capture_output=True`.
- Verified directly (not just "it compiles"): tier lookups for a spread of
  skills, machine-scan against both a dangerous and a clean command, and
  all three `confirm_and_run` outcomes (cancel, run-as-is, edit-then-run)
  executed for real and checked the actual returned output — which is what
  caught the `run_terminal` bug above in the first place.

**Step 7 — Spoken output (TTS), human-toned, English (incl. Indian accent)/
Hindi** — DONE. Implemented in `tts.py`, wired into `daemon.py` and
`main.py`, tested live. Piper `en_IN-spicor` (Indian-accented English,
CPU-only, ~1.1s cold load / ~0.3s per sentence) + Kokoro `hf_alpha`
(Hindi, streaming via RealtimeTTS) + text cleaning (`clean_for_speech`)
and 0.95 speed multiplier for human-like cadence. MeloTTS, Kokoro's
English path, and Punjabi (IndicF5) were all built, benchmarked, and
removed along the way — see Step 7's "What was tried and dropped along
the way, and why" for the full history.

## Build status — 2026-09-15

All seven planned steps are done. This is where the project actually stands,
not an aspirational summary:

**Post-Step-6 bug found from real daily use, not testing:** the first real
user report — a spurious "Listening..." notification right after a
correct action, with nothing real said afterward. Root cause: `daemon.py`'s
`_process_command` (STT + skill resolution + execution) takes real
wall-clock seconds, during which the `sd.InputStream` callback keeps
stuffing fresh chunks into `audio_q` — nothing reads them while processing
is busy. Returning straight to the idle loop afterward burned through that
whole backlog in a tight, non-real-time burst against the wake-word model,
including whatever played *during* processing (e.g. the video the command
itself had just opened). Fixed by draining `audio_q` completely right
after `_process_command` returns, before re-entering idle listening — idle
now only ever scores live audio going forward. Verified live: one real
round-trip (which hit a genuine Gemini API rate limit mid-test, giving a
realistic multi-second processing delay) produced exactly one "Listening..."
notification, no spurious second one afterward.

- **Step 1** — Layer 0 skills engine, 48-skill target (50 registered
  functions), categories A/B/C1-unmarked. DONE.
- **Step 2** — Layer 1 local semantic match (`fastembed`/bge-small),
  tuned against real test cases. DONE.
- **Step 3** — generalized correction layer, reused by app resolution.
  DONE.
- **Step 4** — categories D, E, F, a minimal G, J, L (~55 more skills,
  105 total). CLOSED by explicit scope decision — I and K skipped, the
  full G catalog not built, Layer 1 not extended to these new skills.
- **Step 5** — wake word (`hey jarvis`) + VAD-based turn detection,
  built into `daemon.py`. DONE. Also where the tray icon + always-on
  `systemd --user` services were built (ahead of where this doc
  originally planned them), replacing terminal push-to-talk as the
  real, daily way to run this — installed, enabled, and live on this
  machine right now.
- **Step 6** — safety tier mechanism (registry, machine-scan,
  confirm-loop, always-on audit log). DONE. Activation
  (`SAFETY_TIERS_ENABLED`) stays off by default — that switch is still
  yours to flip, whenever, tier by tier or all at once.
- **Step 7** — spoken output (TTS), human-toned, English (incl. Indian
  accent)/Hindi. DONE. Piper `en_IN-spicor` (Indian English, ~1.1s cold
  load / ~0.3s per sentence, CPU-only) + Kokoro (Hindi, streaming
  ~1.5s time-to-first-chunk). Built with clean markdown stripping, 0.95
  speed multiplier for calm natural tone, and script-based automatic
  language detection. Hinglish explicitly excluded, by your direction.
  MeloTTS, Kokoro's English path, and Punjabi (IndicF5) were all built,
  benchmarked, and replaced along the way — see Step 7's own section for
  the full history, not repeated here.

**What "done" does not mean:** every ⚙️-tagged skill across every
category is still unbuilt (no external accounts were set up), Layer 1
doesn't cover Step 4's ~55 newer skills, categories I and K don't
exist, the tray can't edit a misheard command the way the terminal can,
and tier activation has never actually been turned on and lived with —
only built and unit-verified. The open questions section above is the
honest list of what's still unresolved; it isn't empty because the
build is finished, it's short because most of what was open got
resolved by actually building and testing, one step at a time, instead
of guessing upfront.

**What running this daily actually looks like right now:** the tray icon
and `voice-daemon`/`voice-tray` systemd services, started automatically
at login. Click the tray to talk, or say "hey jarvis." `main.py` remains
for manual/dev testing only. Nothing is gated behind a confirmation
prompt yet — every skill runs immediately on a confident match, SAFE and
DANGEROUS alike — until `SAFETY_TIERS_ENABLED` is turned on.
