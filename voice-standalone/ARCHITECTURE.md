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

**Honest cost:** Layer 1 needs a small local sentence-embedding model (e.g.
all-MiniLM, ~80MB) as a new dependency. Fully local/offline, still millisecond-fast
— but heavier than pure regex. A lighter fallback if that weight isn't wanted yet:
plain keyword/token-overlap scoring, no embedding model, less robust to paraphrasing.

## Latency budget

| Stage | Time |
|---|---|
| STT (Gemini, cloud, unavoidable today) | ~300-1500ms — **the real bottleneck**, not our code |
| Layer 0 grammar scan | <1ms |
| Layer 1 semantic match | ~2-5ms |
| Correction layer | <1ms |
| Execute (dispatch + OS call) | 10-50ms |
| **Total, after transcription, skill-resolved path** | **~10-60ms — genuinely millisecond-scale** |
| Fallback path (adds one more Gemini call) | + ~300-1500ms |

Everything *after* transcription can honestly hit the millisecond goal. STT is the
one piece that can't yet — which is why the Gemini Live API / local-Whisper option
discussed earlier stays the only remaining lever on total latency.

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

## Explicitly deferred / not part of this doc

- Safety tier *activation* (the section above is the mechanism; turning it on
  is a separate future decision).
- Local-only model swap (replacing Gemini with something fully on-device, like
  your friend's build) — a separate decision, not assumed here either way.
- Any NeuroPaca integration — out of scope by design until this proves itself
  standalone.

## Open questions

1. Exact Layer 1 thresholds (0.82 / 0.60 used above) are starting guesses, not
   measured — need real tuning once ~30 skills exist and we can see where
   false-accepts/false-rejects actually happen.
2. Which local embedding model to use for Layer 1 — all-MiniLM is the common
   default, but worth confirming it's fast enough on this machine specifically
   before committing.
3. Where does the "known list" come from for each E-category file skill
   (bookmarks, contacts, folders) — apps and C2 sites are already solved.
4. Which ⚙️-tagged skills are worth the account/API setup at all versus cut —
   e.g. is Telegram integration something you'll actually use, or dead weight
   carried over from Mycroft/OVOS precedent that doesn't fit you?

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

**Step 2 — Benchmark, then add Layer 1 (semantic match)**
- Install a candidate local embedding model (all-MiniLM to start) and actually
  time inference on this machine — answers open question #2 before anything
  is built on top of it.
- Write 3-5 example phrases per skill for the ~56 skills from Step 1.
- Precompute and cache their embeddings at startup; implement the cosine-
  similarity matrix match described in the pipeline diagram.
- Tune the 0.82 / 0.60 thresholds against real test utterances — including
  paraphrased and misheard versions — and measure false-accept/false-reject
  rate before trusting it. Answers open question #1.

**Step 3 — Generalize the correction layer**
- Turn the substring → edit-distance/token-overlap ensemble (already working
  for `open_app`) into one reusable function.
- Apply it to every skill whose argument names a real thing, not just apps.

**Step 4 — Scale toward the full 181, wave by wave**
- Add the rest of the unmarked (buildable-now) skills: D, E, F, G, I, J, K, L.
- Set up each ⚙️-tagged external API/account one at a time, adding its skill
  only once the account exists — cut any that aren't actually worth it
  (open question #4) instead of building them out of obligation.
- Re-check the Layer 1 thresholds as skill count grows — more skills means
  more chances for two of them to look similar in vector space.

**Step 5 — Wake word + turn detection**
- Add hotword detection inside the Capture module; push-to-talk stays as the
  permanent manual fallback.
- Add the LiveKit-style turn-detection model for wake-word sessions only.

**Step 6 — Safety tiers (only once daily-used and stable)**
- Tag every skill with its static tier (🔒 ones in the catalog are the
  DANGEROUS candidates already flagged).
- Build the confirm-loop generator, the editable preview, the machine-scan
  tripwire, and the Audit Log module.
- Turn tiers on one at a time — timing is entirely your call.
