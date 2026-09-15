# Step 7 Build Guide — Giving the Assistant a Voice

This is a hands-on, do-it-yourself guide to building Step 7 of
`voice-standalone`: making the assistant actually **speak**, in a natural
human tone, in English (with an Indian accent as the eventual default),
Hindi, and Punjabi. It assumes you have the repo checked out and Steps 1-6
already working (they are — this guide only adds to what exists).

Every technical claim in here (package names, function signatures, install
commands) was verified directly against the real project's own
documentation before this guide was written — not guessed. Where something
genuinely isn't known yet (mostly: exact speed on *this* machine), that's
called out honestly as something you'll measure yourself, not asserted as
fact.

**The one rule that matters more than any other step below: test each
stage for real before moving to the next one.** Every step in this whole
project so far was built this way — write a small piece, run it, listen to
it or read its real output, fix what's wrong, *then* move on. Building all
five stages first and testing at the end is how ten small bugs turn into
one confusing mess. Don't do that.

---

## 0. What you're actually building, in plain words

Right now, when you say "hey jarvis, open YouTube and play music," the
assistant does it and sends you a silent desktop notification. After Step
7, it will also **say something out loud** — "Playing music on YouTube" —
at the same time. If you ask something that takes a moment to answer, like
"what does ephemeral mean," it should say something like "let me check"
immediately, then speak the actual answer once it has it, instead of going
quiet for a few seconds.

You're adding one new file (`tts.py`) that turns text into spoken audio,
and wiring it into the two places that already produce text results:
`daemon.py` (the real, tray+wake-word-driven daemon) and, optionally,
`main.py` (the manual/dev-testing mode).

**Three separate "voices" get built, for three separate purposes:**

| Language | Engine | Why this one |
|---|---|---|
| English | Kokoro (via RealtimeTTS) | Fast, streaming, proven quality |
| Hindi | Kokoro (same engine, different voice) | Kokoro genuinely has real Hindi voices |
| Indian-accented English | MeloTTS | Kokoro's English is American/British only; this is the one with a real Indian accent |
| Punjabi | AI4Bharat's IndicF5 | The one language none of the "easy" options support at all |

You'll build these **in that order**, one at a time, listening to each
before starting the next.

---

## 1. Before you start

Open a terminal in `voice-standalone/` and make sure you're using the
project's own virtual environment for everything below (`.venv/bin/pip`,
`.venv/bin/python`), the same as every other part of this project — not
your system Python.

Check your speakers work at all first (obvious, but worth 10 seconds):
```
speaker-test -t wav -c 2 -l 1
```
If you don't hear anything, fix that before writing a single line of TTS
code — otherwise you'll be debugging the wrong thing.

---

## 2. Stage 1 — English voice (Kokoro + RealtimeTTS)

**What "RealtimeTTS" means, in plain words:** it's a library that starts
playing audio *while* the rest of the sentence is still being generated,
instead of waiting for the whole thing to finish first. That's the
difference between the assistant feeling instant vs. feeling like it's
"thinking" before every sentence.

### 2.1 Install

```
.venv/bin/pip install "realtimetts[kokoro]"
```

This pulls in RealtimeTTS itself plus everything the Kokoro engine
specifically needs.

### 2.2 Write `tts.py`

Create `voice-standalone/tts.py`:

```python
"""
tts.py — spoken output. Thin wrapper around RealtimeTTS/KokoroEngine, same
pattern as stt.py (a thin wrapper around Gemini's transcription call).
"""

from RealtimeTTS import TextToAudioStream, KokoroEngine

# One engine instance, reused across calls — creating a new one per
# sentence would reload the model every time, which is slow. "af_heart" is
# Kokoro's default American English voice; you'll add more voices in later
# stages, not by creating new engines each time, but by switching this.
_engine = KokoroEngine(voice="af_heart")
_stream = TextToAudioStream(_engine)


def speak(text: str) -> None:
    """Speak text out loud. Blocks until speech finishes — that's fine for
    now; daemon.py already runs one command at a time."""
    if not text.strip():
        return
    _stream.feed(text)
    _stream.play()
```

### 2.3 Test it standalone — BEFORE touching daemon.py

Run this directly:
```
.venv/bin/python -c "from tts import speak; speak('Hello, this is a test of the voice assistant speaking out loud.')"
```

**Checklist — do not proceed until every one of these is true:**
- [ ] You actually heard it speak (not just "no error appeared" — actually
      heard words come out of your speakers).
- [ ] It didn't take more than ~1-2 seconds to *start* speaking (that's the
      "streaming" benefit — if it took much longer, something's wrong with
      the streaming setup, not just slow hardware).
- [ ] The words were correct and understandable, not garbled.

If any of those fail, fix it now. Common real problems to check: is a
different audio output device selected than you expect (check your
system's sound settings), is the `.venv` you're running actually the one
you installed `realtimetts[kokoro]` into (a classic Python mistake — two
different virtual environments existing and installing into the wrong
one), did the model actually download (first run needs internet to fetch
Kokoro's model files — check for a real error message, not just silence).

### 2.4 Wire it into `daemon.py`

Open `daemon.py`. Find `_process_command` — this is where a result becomes
a desktop notification via `_notify(...)`. Import `tts` at the top of the
file, and add a `speak()` call alongside each `_notify()` call, not instead
of it — keep the notifications, since you'll still want a visible record
of what happened.

For the "immediate interim phrase" behavior you asked for originally
(speaking "let me check" *before* a slow action finishes): add a `speak()`
call right after the skill is resolved (you know what it's *going* to do)
but before `actions.DISPATCH[name](args)` actually runs, only for skills
you know are slow (web searches, anything hitting `llm_intent`). A simple
first version: speak an interim phrase whenever the result came from the
LLM fallback (`llm` layer) rather than the fast local layers, since that's
already your signal for "this took longer."

Then, after the action's real output is captured (the existing
`buffer.getvalue()` logic already does this), speak that final result too.

### 2.5 Test the full daemon path

Restart the real service:
```
systemctl --user restart voice-daemon
```
Say "hey jarvis, what time is it" out loud, or click the tray and speak
it. **Listen for the actual spoken response**, not just the notification.

**Checklist:**
- [ ] It spoke the result out loud, correctly.
- [ ] It didn't speak twice, or cut itself off mid-sentence.
- [ ] Saying something else right after didn't crash the daemon (check
      `systemctl --user status voice-daemon` — still "active (running)").
- [ ] The existing notification still also appeared — you didn't
      accidentally replace one feedback mechanism with the other.

Only once all four are true, move to Stage 2.

---

## 3. Stage 2 — Hindi voice (same engine, different voice)

Kokoro's real Hindi voices are `hf_alpha`, `hf_beta` (female) and
`hm_omega`, `hm_psi` (male) — verified directly against Kokoro's own voice
list, not assumed.

### 3.1 Add a language-to-voice mapping

In `tts.py`, instead of one hardcoded engine, support switching voices:

```python
_VOICE_BY_LANG = {
    "en": "af_heart",
    "hi": "hf_alpha",
}

def speak(text: str, lang: str = "en") -> None:
    if not text.strip():
        return
    _engine.set_voice(_VOICE_BY_LANG.get(lang, "af_heart"))
    _stream.feed(text)
    _stream.play()
```

(Check RealtimeTTS's actual `KokoroEngine` for whether the method is
called `set_voice` or something else at the version you installed — if it
errors, print `dir(_engine)` and look for the real method name rather than
guessing; library APIs do change between versions.)

### 3.2 Test it

```
.venv/bin/python -c "from tts import speak; speak('नमस्ते, यह एक परीक्षण है', lang='hi')"
```

**Checklist:**
- [ ] You heard real Hindi speech, not English text read with an accent,
      and not silence/an error.
- [ ] It sounded like the *right* language's rhythm and sound, not garbled.
- [ ] Switching back to `lang="en"` right after still works (proves
      switching voices on the same engine instance doesn't break anything).

If Hindi text typed directly into your terminal shows as `?????` or
similar, that's a terminal encoding issue, not a bug in your code — save
the test text in a `.py` file with `# -*- coding: utf-8 -*-` at the top
instead of typing Hindi directly at a shell prompt.

---

## 4. Stage 3 — Indian-accented English (MeloTTS)

This is the one you specifically asked for. Kokoro's English is only
American or British — MeloTTS has a real, separate Indian-English voice.

**Important honest note:** MeloTTS has no RealtimeTTS engine (verified —
it's not in RealtimeTTS's supported engine list). That means this path is
NOT streaming — it generates the whole sentence's audio first, then plays
it, unlike Kokoro's near-instant start. You are trading a bit of
speed for the more natural accent. That's a real tradeoff to notice while
testing, not something to ignore.

### 4.1 Install

MeloTTS isn't a simple `pip install` — it needs a git clone:
```
cd /home/bhanot/NeuroPaca/voice-standalone
git clone https://github.com/myshell-ai/MeloTTS.git melo_tts_src
cd melo_tts_src
../.venv/bin/pip install -e .
../.venv/bin/python -m unidic download
cd ..
```

### 4.2 Test the engine standalone first

```
.venv/bin/python -c "
from melo.api import TTS
model = TTS(language='EN', device='auto')
speaker_ids = model.hps.data.spk2id
model.tts_to_file('Testing the Indian English voice.', speaker_ids['EN_INDIA'], 'test_en_india.wav', speed=1.0)
print('done, saved to test_en_india.wav')
"
aplay test_en_india.wav   # or: xdg-open test_en_india.wav
rm test_en_india.wav
```

**Checklist:**
- [ ] The file was created and actually contains speech (not 0 bytes, not
      silence).
- [ ] It genuinely sounds like an Indian English accent, not American/
      British — listen for this specifically, don't just assume the label
      is correct.
- [ ] **Time it.** Wrap the `model.tts_to_file(...)` call with
      `time.perf_counter()` before and after, for a few different sentence
      lengths. This number doesn't exist published anywhere — you are the
      first to measure it for this exact use case. Write down what you get.

### 4.3 Decide: is this fast enough to be the *default* English voice?

This is a real decision point, not a foregone conclusion. If a normal
response sentence takes, say, under 1-2 seconds to generate, it's probably
fine to make `EN_INDIA` the default instead of Kokoro's `af_heart`. If it
takes much longer than that, keep Kokoro as the fast default and treat
MeloTTS as an optional "sounds better but slower" mode you can switch to
later, or investigate whether a GPU is available on this machine (MeloTTS
supports `device='cuda'` if so) before giving up on making it the default.

### 4.4 Wire it into `tts.py`

Add a separate function (since the API is completely different from
RealtimeTTS/Kokoro — this is a case where forcing one unified interface too
early would hide a real, meaningful difference):

```python
from melo.api import TTS as _MeloTTS
import tempfile
import subprocess

_melo_model = None  # lazy-loaded — don't pay MeloTTS's load time if it's never used

def _get_melo():
    global _melo_model
    if _melo_model is None:
        _melo_model = _MeloTTS(language='EN', device='auto')
    return _melo_model


def speak_indian_english(text: str) -> None:
    if not text.strip():
        return
    model = _get_melo()
    speaker_ids = model.hps.data.spk2id
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        path = tmp.name
    model.tts_to_file(text, speaker_ids['EN_INDIA'], path, speed=1.0)
    subprocess.run(["aplay", path], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import os
    os.unlink(path)
```

If you decided in 4.3 to make this the real default, update `speak()`
itself to call this when `lang="en"` instead of Kokoro's `af_heart`; if
not, leave both available and pick which one `daemon.py` calls via a
config flag (`config.TTS_ENGLISH_VOICE = "kokoro"` or `"melo"`).

### 4.5 Test through the full daemon path

Same as Stage 1.5 — restart the service, trigger a real command, listen.

**Checklist:**
- [ ] It actually spoke with the Indian accent (if you made it default) or
      the old Kokoro voice still works (if you didn't).
- [ ] The daemon didn't hang or crash waiting for MeloTTS to generate.
- [ ] If it's noticeably slower, that delay is *acceptable* to you in
      practice — say a few real commands and judge it yourself, don't just
      trust the benchmark number in isolation.

---

## 5. Stage 4 — Punjabi (IndicF5)

**Important honest note before you start:** IndicF5 is a *voice cloning*
style model — verified directly from its own documentation. It doesn't
just take text; it needs a **reference audio sample** (a real recording of
someone speaking Punjabi) plus the exact text of what that reference
recording says, and it copies that speaker's voice characteristics for the
new sentence. This is different from Kokoro/MeloTTS, which have their own
built-in voices you just select by name.

### 5.1 Install

```
.venv/bin/pip install git+https://github.com/ai4bharat/IndicF5.git
```
(If this fails due to a dependency conflict with the main project's
`.venv`, that's worth knowing and reporting — this one may genuinely need
its own separate virtual environment, similar to how `tray.py` needed
system Python instead of the project's own venv for unrelated reasons.
Don't force it if it breaks other packages; isolate it instead.)

### 5.2 Get a reference audio sample

Go to `huggingface.co/ai4bharat/IndicF5` and look in its `prompts/`
directory for a Punjabi example (something like `PAN_F_HAPPY_00001.wav`,
alongside a text file with the exact transcript of what it says). Download
both into `voice-standalone/tts_reference_audio/`.

### 5.3 Test the engine standalone first

```
.venv/bin/python -c "
import time
from transformers import AutoModel
import numpy as np
import soundfile as sf

model = AutoModel.from_pretrained('ai4bharat/IndicF5', trust_remote_code=True)

t0 = time.perf_counter()
audio = model(
    'ਸਤਿ ਸ੍ਰੀ ਅਕਾਲ, ਇਹ ਇੱਕ ਟੈਸਟ ਹੈ',
    ref_audio_path='tts_reference_audio/PAN_F_HAPPY_00001.wav',
    ref_text='<the exact transcript text that came with that reference file>',
)
t1 = time.perf_counter()
print(f'generation took {t1-t0:.2f}s')

audio = audio.astype(np.float32) / 32768.0 if audio.dtype == np.int16 else audio
sf.write('test_punjabi.wav', np.array(audio, dtype=np.float32), samplerate=24000)
"
aplay test_punjabi.wav
rm test_punjabi.wav
```

**Checklist:**
- [ ] You hear real Punjabi speech.
- [ ] It doesn't just sound like a robotic copy of the reference clip — it
      says your NEW sentence, not the reference sentence.
- [ ] Write down the generation time, same as MeloTTS. Compare it honestly
      to Kokoro's near-instant streaming — this is very likely the slowest
      of the three voices, and that's expected, not a sign you did
      something wrong.

### 5.4 Wire it into `tts.py`

```python
_indicf5_model = None
_PUNJABI_REF_AUDIO = "tts_reference_audio/PAN_F_HAPPY_00001.wav"
_PUNJABI_REF_TEXT = "<the exact transcript text that came with that reference file>"

def _get_indicf5():
    global _indicf5_model
    if _indicf5_model is None:
        from transformers import AutoModel
        _indicf5_model = AutoModel.from_pretrained("ai4bharat/IndicF5", trust_remote_code=True)
    return _indicf5_model


def speak_punjabi(text: str) -> None:
    if not text.strip():
        return
    import numpy as np
    import soundfile as sf
    import subprocess
    import tempfile
    import os

    model = _get_indicf5()
    audio = model(text, ref_audio_path=_PUNJABI_REF_AUDIO, ref_text=_PUNJABI_REF_TEXT)
    audio = audio.astype(np.float32) / 32768.0 if audio.dtype == np.int16 else audio
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        path = tmp.name
    sf.write(path, np.array(audio, dtype=np.float32), samplerate=24000)
    subprocess.run(["aplay", path], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.unlink(path)
```

Add `"pa": speak_punjabi` handling into your main `speak(text, lang)`
dispatcher (a simple `if lang == "pa": return speak_punjabi(text)` at the
top of `speak()` works fine).

### 5.5 Decide if it's usable live at all

If generation takes, say, more than 3-4 seconds for a normal sentence,
seriously consider: is this actually usable for a live voice assistant, or
is it better to only use Punjabi output for short phrases, or skip it for
now and revisit once you find a faster option? **This is explicitly okay
to conclude** — the project's own architecture doc already flagged this as
a real open question, not a guaranteed win. Report what you actually
measure honestly rather than forcing it in if it feels bad to use.

---

## 6. Stage 5 — Making it sound human, not robotic

**An honest correction to the earlier high-level research:** the original
plan mentioned "SSML tags" (`<break>`, `<prosody>`, etc.) for humanizing
speech. SSML is real, but it's mainly a convention used by *cloud* TTS
APIs (Google, Azure, Amazon) — the three *local* engines you just built
(Kokoro, MeloTTS, IndicF5) do **not** parse SSML tags. Feeding them literal
`<break time="300ms"/>` text will make them read the tag out loud as
words, which is the opposite of what you want. Here's what actually
applies to the engines you're using:

- **Punctuation is your pause control.** All three respect commas, periods,
  and question marks reasonably naturally already — the actual technique
  is *writing the text you hand to speak() the way a person would actually
  say it*, with real punctuation at real pause points, rather than one run-on
  sentence. If you want a longer pause than a comma gives, an ellipsis
  (`...`) or splitting into two separate `speak()` calls with a tiny delay
  between them both work in practice — try both and use whichever sounds
  better to your own ear.
- **Speed matters more than you'd think.** Both Kokoro (`default_speed`)
  and MeloTTS (`speed=`) let you set a speed multiplier. The default is
  usually close to right, but slightly *under* 1.0 (like 0.92-0.95) often
  sounds calmer and more natural than the raw default — try a few values
  by ear, this is subjective and worth just listening to.
- **For IndicF5 specifically, naturalness comes from the reference audio.**
  Since it clones the reference speaker's delivery, picking (or recording)
  a reference clip that already sounds warm and natural matters more than
  any code change — if the built-in AI4Bharat sample sounds robotic, try a
  different one from their `prompts/` directory before assuming the model
  itself is the problem.
- **Don't say too much.** A long, dense paragraph read aloud, even by a
  good voice, sounds worse than a short, clear sentence. When you wire
  results into `speak()`, consider trimming very long outputs (like a full
  `ps aux` dump) down to a short spoken summary, and let the detailed
  version stay in the notification/log instead — the assistant should
  *tell* you the gist, not read you a report.

This whole stage is inherently subjective. There's no code checklist that
proves "this sounds human" — you have to actually listen, more than once,
in a normal room with normal background noise, and trust your own
judgment over any theoretical technique above.

---

## 7. The bug-hunting loop — do this after every stage, and again at the end

This is the same discipline used to build every other step of this
project. Don't skip it because a stage "seems to work."

**For each stage, right after you finish it, go through this list:**

1. **Does it still do what it did before?** Run the existing test suite:
   ```
   .venv/bin/python smoke_test_skills.py
   ```
   If this shows anything other than "176 passed, 0 failed," you broke
   something unrelated — find out what changed and fix it before
   continuing, even if it seems unconnected to TTS.

2. **Does the daemon survive a real error?** Deliberately break something
   temporarily — rename `tts.py` to `tts_broken.py` for a moment, restart
   the daemon, and confirm it still starts and still executes actions (just
   without speaking) instead of crashing entirely. Then rename it back.
   This proves your TTS code is wrapped in the daemon's existing
   try/except safety net, not silently exempt from it. If the daemon
   crashes instead of degrading gracefully, wrap the `speak()` call sites
   in their own `try/except Exception` that logs the error and continues,
   the same pattern every other risky call in `daemon.py` already uses.

3. **Does it handle silence/empty text?** Call `speak("")` and
   `speak("   ")` directly — confirm nothing crashes and nothing plays.
   (The example code above already guards this, but verify it, don't
   trust it blindly.)

4. **Does it handle a very long result?** Feed `speak()` a few hundred
   words of text (paste a long paragraph). Confirm it doesn't hang forever,
   crash, or produce garbled audio partway through.

5. **Say five real, different commands out loud, back to back**, the way
   you'd actually use it in a normal session — not just the one example
   you tested with. Listen for: wrong language selected, wrong voice
   selected, audio cutting off early, audio overlapping with the next
   command's notification, or any awkward silence where you expected
   speech.

6. **Check the daemon is still alive after all of the above:**
   ```
   systemctl --user status voice-daemon
   ```
   Should say "active (running)," not "failed" or "inactive."

**Write down every real bug you find**, even small ones, before fixing it
— a one-line note like "Hindi voice spoke English numbers in English
instead of Hindi" is enough. Fix it, then re-run the *entire* checklist
above again from step 1, not just the part that failed — a fix can break
something else that was previously fine, and the only way to catch that is
re-checking everything, the same discipline this whole project has used
throughout.

**Repeat this whole loop until you go through it once with zero new bugs
found.** That's what "done" means here — not "I think it's fine," but "I
ran the full checklist and found nothing new to fix."

### After all five stages are built, do one final full-system pass:

- Run the smoke test suite one more time.
- Say at least one real command in each language (English, Hindi, Punjabi,
  and the Indian-English voice if you made it default) in the same
  session, back to back, without restarting the daemon in between —
  confirm switching between them works cleanly and nothing from one
  language "leaks" into another (e.g. Hindi voice still selected when it
  should have switched back to English).
- Let the daemon run normally, doing normal things, for at least 15-20
  minutes of real use — not a rushed test — since some bugs (like the
  audio-queue backlog bug found after Step 6) only show up from genuine
  daily use, not quick isolated tests.
- Only once ALL of that is clean: commit your changes, and update
  `ARCHITECTURE.md`'s Step 7 section to say DONE instead of PLANNED, with
  the real numbers you measured (actual generation times for MeloTTS and
  IndicF5) replacing the "unverified, needs benchmarking" language that's
  there now — that's the same standard every other step in that document
  was held to.
