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

## Explicitly deferred / not part of this doc

- Safety tier *activation* (the section above is the mechanism; turning it on
  is a separate future decision).
- Local-only model swap (replacing Gemini with something fully on-device, like
  your friend's build) — a separate decision, not assumed here either way.
- Any NeuroPaca integration — out of scope by design until this proves itself
  standalone.

## Open questions

1. ~~Exact Layer 1 thresholds~~ — RESOLVED (Step 2): 0.75/0.55, tuned against
   58 real independent test cases (`smoke_test_semantic.py`). Correct-match
   and reject-worthy scores overlap in a 0.71-0.75 band for this model, so no
   threshold gets everything right — chose zero false-accepts over zero
   false-rejects. Revisit if the skill catalog grows enough to shift this band.
2. ~~Which local embedding model to use for Layer 1~~ — RESOLVED (Step 2):
   `fastembed` + `BAAI/bge-small-en-v1.5`, benchmarked live against
   `sentence-transformers`/all-MiniLM (see Layer 1's "Honest cost" note above).
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

**Step 5 — Wake word + turn detection**
- Add hotword detection inside `daemon.py` (see above — this is now where
  the Capture module's logic actually lives); push-to-talk (both `main.py`'s
  dev mode and the tray's click-to-record) stays as the permanent manual
  fallback.
- Add the LiveKit-style turn-detection model for wake-word sessions only.

**Step 6 — Safety tiers (only once daily-used and stable)**
- Tag every skill with its static tier (🔒 ones in the catalog are the
  DANGEROUS candidates already flagged).
- Build the confirm-loop generator, the editable preview, the machine-scan
  tripwire, and the Audit Log module.
- Turn tiers on one at a time — timing is entirely your call.
