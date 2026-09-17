#!/usr/bin/env python3
"""
Smoke test for skills/ Layer-0 matchers.

Runs every matcher against:
  - 2-3 realistic phrasings per skill
  - 1-2 deliberately odd/edge-case phrasings per skill
  - Selected "should NOT match" probes (false-positive traps)

Exit code: 0 on pass, 1 if any expectation is violated.

Usage (from voice-standalone/):
    .venv/bin/python smoke_test_skills.py
"""

import sys
import os

# Run from voice-standalone/ directory
sys.path.insert(0, os.path.dirname(__file__))

from skills import match_skill

PASS = 0
FAIL = 0
_results: list[str] = []


def expect_match(text: str, expected_name: str, description: str = "") -> None:
    global PASS, FAIL
    name, args = match_skill(text)
    if name == expected_name:
        PASS += 1
        _results.append(f"  ✓  [{expected_name}]  {text!r}")
    else:
        FAIL += 1
        _results.append(
            f"  ✗  [{expected_name}] EXPECTED but got [{name}({args})]  →  {text!r}"
            + (f"  ({description})" if description else "")
        )


def expect_no_match(text: str, description: str = "") -> None:
    global PASS, FAIL
    name, args = match_skill(text)
    if name is None:
        PASS += 1
        _results.append(f"  ✓  [no match]  {text!r}")
    else:
        FAIL += 1
        _results.append(
            f"  ✗  [no match] EXPECTED but got [{name}({args})]  →  {text!r}"
            + (f"  ({description})" if description else "")
        )


# ---------------------------------------------------------------------------
# ─── Category A — System & power control ───────────────────────────────────
# ---------------------------------------------------------------------------

print("\n=== A — System & power control ===")

# A01 volume_up
expect_match("volume up", "volume_up")
expect_match("turn up the volume", "volume_up")
expect_match("increase volume", "volume_up")
expect_match("VOLUME UP please", "volume_up", "case insensitive")

# A02 volume_down
expect_match("volume down", "volume_down")
expect_match("lower the volume", "volume_down")
expect_match("turn down the volume", "volume_down")

# A03 mute  — must NOT fire on "mute mic" (A25) or "unmute" (A04)
expect_match("mute", "mute")
expect_match("mute the volume", "mute")
expect_match("mute audio", "mute")
# "mute the microphone" should go to toggle_mic_mute, NOT mute
expect_match("mute the microphone", "toggle_mic_mute", "mic keyword → toggle_mic_mute, not mute")
# "unmute" should go to unmute, NOT mute
expect_match("unmute", "unmute", "unmute → unmute skill, not mute")

# A04 unmute
expect_match("unmute", "unmute")
expect_match("unmute the volume", "unmute")

# A05 set_volume
expect_match("set volume to 60", "set_volume")
expect_match("volume 70 percent", "set_volume")
expect_match("set volume to 100%", "set_volume")
expect_match("volume at 0", "set_volume")
expect_no_match("volume 150", "out of range — should not match")
expect_no_match("volume minus 10", "non-numeric — should not match")

# A06 brightness_up
expect_match("brightness up", "brightness_up")
expect_match("increase brightness", "brightness_up")
expect_match("turn up the brightness", "brightness_up")

# A07 brightness_down
expect_match("brightness down", "brightness_down")
expect_match("dim the screen", "brightness_down")
expect_match("lower the brightness", "brightness_down")

# A08 set_brightness
expect_match("set brightness to 80", "set_brightness")
expect_match("brightness 50 percent", "set_brightness")
expect_match("set brightness to 10%", "set_brightness")

# A09 toggle_dark_mode
expect_match("dark mode", "toggle_dark_mode")
expect_match("switch to dark mode", "toggle_dark_mode")
expect_match("enable light mode", "toggle_dark_mode")
expect_match("toggle dark theme", "toggle_dark_mode")

# A10 lock_screen
expect_match("lock screen", "lock_screen")
expect_match("lock the screen", "lock_screen")
expect_match("lock my computer", "lock_screen")
expect_match("screen lock", "lock_screen")

# A11 shutdown
expect_match("shut down", "shutdown")
expect_match("power off", "shutdown")
expect_match("turn off the computer", "shutdown")
expect_match("shutdown the system", "shutdown")

# A12 restart
expect_match("restart the computer", "restart")
expect_match("reboot", "restart")
expect_match("restart my laptop", "restart")
# Step 4 added F14 restart_service — this correctly matches THAT skill now,
# not "no match." The important property (A12 not stealing it) still holds.
expect_match("restart the apache service", "restart_service", "service restart — must be F14, not A12")

# A13 logout
expect_match("log out", "logout")
expect_match("logout", "logout")
expect_match("sign out", "logout")

# A14 sleep
expect_match("sleep", "sleep")
expect_match("suspend the computer", "sleep")
expect_match("put the computer to sleep", "sleep")

# A15 screenshot
expect_match("take a screenshot", "screenshot")
expect_match("screenshot", "screenshot")
expect_match("capture the screen", "screenshot")
expect_match("screen grab", "screenshot")

# A16 start_recording
expect_match("start screen recording", "start_recording")
expect_match("record the screen", "start_recording")
expect_match("begin recording", "start_recording")

# A17 stop_recording
expect_match("stop screen recording", "stop_recording")
expect_match("stop recording", "stop_recording")
expect_match("end recording", "stop_recording")

# A18 toggle_wifi
expect_match("turn off wifi", "toggle_wifi")
expect_match("wifi on", "toggle_wifi")
expect_match("toggle wifi", "toggle_wifi")
expect_match("turn on wi-fi", "toggle_wifi")

# A19 toggle_bluetooth
expect_match("bluetooth off", "toggle_bluetooth")
expect_match("turn on bluetooth", "toggle_bluetooth")
expect_match("toggle bluetooth", "toggle_bluetooth")

# A20 toggle_dnd
expect_match("do not disturb", "toggle_dnd")
expect_match("enable do not disturb", "toggle_dnd")
expect_match("notifications off", "toggle_dnd")
expect_match("disable notifications", "toggle_dnd")
expect_match("DND on", "toggle_dnd")

# A21 toggle_airplane
expect_match("airplane mode on", "toggle_airplane")
expect_match("toggle airplane mode", "toggle_airplane")
expect_match("flight mode", "toggle_airplane")

# A22 toggle_night_light
expect_match("night light on", "toggle_night_light")
expect_match("blue light filter", "toggle_night_light")
expect_match("toggle night mode", "toggle_night_light")
expect_match("warm display on", "toggle_night_light")

# A23 battery_status
expect_match("battery status", "battery_status")
expect_match("how much battery", "battery_status")
expect_match("check my battery", "battery_status")
expect_match("is my laptop charging", "battery_status")

# A24 toggle_kbd_backlight
expect_match("keyboard backlight on", "toggle_kbd_backlight")
expect_match("toggle keyboard backlight", "toggle_kbd_backlight")
expect_match("keyboard light off", "toggle_kbd_backlight")

# A25 toggle_mic_mute
expect_match("mute microphone", "toggle_mic_mute")
expect_match("mute mic", "toggle_mic_mute")
expect_match("unmute microphone", "toggle_mic_mute")
expect_match("mic mute toggle", "toggle_mic_mute")

# A26 toggle_display
expect_match("toggle external display", "toggle_display")
expect_match("mirror display", "toggle_display")
expect_match("extend the display", "toggle_display")
expect_match("second monitor", "toggle_display")

# ---------------------------------------------------------------------------
# ─── Category B — App & window management ──────────────────────────────────
# ---------------------------------------------------------------------------

print("\n=== B — App & window management ===")

# B01 open_app — use apps confirmed to be installed on this system
expect_match("open code", "open_app")
expect_match("launch visual studio code", "open_app")
expect_match("open cosmic files", "open_app")
expect_no_match("open a can of worms", "unknown app — should return None, not guess")

# B02 close_app
expect_match("close code", "close_app")
expect_match("quit gedit", "close_app")
expect_no_match("close everything", "ambiguous 'all' — should not match")

# B03 switch_to_app
expect_match("switch to code", "switch_to_app")
expect_match("go to code", "switch_to_app")
expect_match("focus gedit", "switch_to_app")

# B04 list_open_apps
expect_match("list open apps", "list_open_apps")
expect_match("what's running", "list_open_apps")
expect_match("show open applications", "list_open_apps")

# B05 switch_workspace
expect_match("switch to workspace 2", "switch_workspace")
expect_match("go to workspace three", "switch_workspace")
expect_match("workspace 1", "switch_workspace")

# B06 show_desktop
expect_match("show desktop", "show_desktop")
expect_match("minimize all windows", "show_desktop")

# B07 maximize_window
expect_match("maximize the window", "maximize_window")
expect_match("fullscreen the current app", "maximize_window")

# B08 force_quit
expect_match("force quit code", "force_quit")
expect_match("kill code", "force_quit")

# B09 open_launcher
expect_match("open launcher", "open_launcher")
expect_match("open app launcher", "open_launcher")
expect_match("show apps", "open_launcher")

# B10 reopen_last_closed
expect_match("reopen last closed app", "reopen_last_closed")
expect_match("restore previous window", "reopen_last_closed")

# ---------------------------------------------------------------------------
# ─── Category E — Files & filesystem ───────────────────────────────────────
# ---------------------------------------------------------------------------

print("\n=== E — Files & filesystem ===")

expect_match("List all the folders I have on desktop", "list_desktop_folders")
expect_match("list desktop folders", "list_desktop_folders")
expect_match("show desktop files", "list_desktop_folders")
expect_match("what folders are on my desktop", "list_desktop_folders")
expect_match("ls desktop", "list_desktop_folders")
expect_match("find a file called report.txt", "find_file")
expect_match("open the file notes.md", "open_file")
expect_match("open my documents folder", "open_folder")
expect_match("list files in downloads", "list_files")

# ---------------------------------------------------------------------------
# ─── Category C1 — Web search & general knowledge ──────────────────────────
# ---------------------------------------------------------------------------

print("\n=== C1 — Web search & general knowledge ===")

# C02 youtube_search (before google_search)
expect_match("search for cats on youtube", "youtube_search")
expect_match("youtube search for lo-fi music", "youtube_search")
expect_match("find tutorials on youtube", "youtube_search")
expect_match("play piano on youtube", "youtube_search")

# C03 wikipedia_search
expect_match("wikipedia python programming", "wikipedia_search")
expect_match("look up quantum mechanics on wikipedia", "wikipedia_search")
expect_match("wiki Albert Einstein", "wikipedia_search")

# C04 duckduckgo_search
expect_match("search for privacy tips on duckduckgo", "duckduckgo_search")
expect_match("duckduckgo linux mint", "duckduckgo_search")

# C05 define_word
expect_match("define serendipity", "define_word")
expect_match("what does ephemeral mean", "define_word")
expect_match("meaning of the word ubiquitous", "define_word")

# C06 spell_word
expect_match("how do you spell necessary", "spell_word")
expect_match("spell accommodation", "spell_word")
expect_match("spelling of bureaucracy", "spell_word")

# C07 get_date
expect_match("what's the date", "get_date")
expect_match("what day is it today", "get_date")
expect_match("today's date", "get_date")

# C08 get_time (should NOT match timezone queries — get_time excludes "in <location>" phrasing)
expect_match("what time is it", "get_time")
expect_match("current time", "get_time")
# "what time is it in Tokyo" correctly falls through get_time's exclusion → timezone_conversion catches it

# C09 timezone_conversion
expect_match("what time is it in Tokyo", "timezone_conversion")
expect_match("time in New York", "timezone_conversion")
expect_match("what's the time in London", "timezone_conversion")

# C10 unit_conversion
expect_match("convert 5 km to miles", "unit_conversion")
expect_match("100 fahrenheit to celsius", "unit_conversion")
expect_match("convert 10 kilograms to pounds", "unit_conversion")

# C11 calculator
expect_match("calculate 42 times 7", "calculator")
expect_match("what is 100 divided by 4", "calculator")
expect_match("compute 2 to the power of 10", "calculator")
expect_no_match("what is the weather today", "should not match calculator")

# C12 my_ip_address
expect_match("what's my IP", "my_ip_address")
expect_match("what is my ip address", "my_ip_address")
expect_match("my public ip", "my_ip_address")

# C13 speed_test
expect_match("run a speed test", "speed_test")
expect_match("test my internet speed", "speed_test")
expect_match("speed test", "speed_test")

# C14 today_in_history
expect_match("what happened today in history", "today_in_history")
expect_match("on this day in history", "today_in_history")
expect_match("today in history", "today_in_history")

# C01 google_search (explicit keywords only)
expect_match("search for python tutorials", "google_search")
expect_match("google the best coffee shops", "google_search")
expect_match("search the web for Linux tips", "google_search")
expect_match("look up quantum physics on google", "google_search")
# These should NOT be stolen by google_search — they match more specific ones
expect_match("search for music on youtube", "youtube_search",
             "youtube before google — key ordering test")
expect_match("search for gravity on wikipedia", "wikipedia_search",
             "wikipedia before google")

# ---------------------------------------------------------------------------
# ─── Category G — Productivity ─────────────────────────────────────────────
# ---------------------------------------------------------------------------

print("\n=== G — Productivity ===")

# G01 add_todo
expect_match("add a to-do: buy milk", "add_todo")
expect_match("remind me to call mom", "add_todo")

# G02 list_todos
expect_match("list my to-dos", "list_todos")
expect_match("what's on my to-do list", "list_todos")

# ---------------------------------------------------------------------------
# ─── Category H — Communication ───────────────────────────────────────────
# ---------------------------------------------------------------------------

print("\n=== H — Communication ===")

# H01 read_latest_emails
expect_match("check my latest emails", "read_latest_emails")
expect_match("read my last 3 emails", "read_latest_emails")
expect_match("any email from swatik", "read_latest_emails")
expect_match("check emails from groww", "read_latest_emails")
expect_match("search emails for IPO", "read_latest_emails")
expect_match("read 2nd recent email", "read_latest_emails")
expect_match("read email 3", "read_latest_emails")
expect_match("read the first email from swatik", "read_latest_emails")

# H02 send_email
expect_match("send email to test@example.com with subject Hi and message hello", "send_email")
expect_match("send an email to team@company.com saying please review", "send_email")

# ---------------------------------------------------------------------------
# ─── Category K — Small-talk & conversation ────────────────────────────────
# ---------------------------------------------------------------------------

print("\n=== K — Small-talk & conversation ===")

expect_match("how are you", "small_talk")
expect_match("hello how are you", "small_talk")
expect_match("hello", "small_talk")
expect_match("hi", "small_talk")
expect_match("good morning", "small_talk")
expect_match("what's up", "small_talk")
expect_match("who are you", "small_talk")
expect_match("Hello my name is Jatin, how are you doing?", "small_talk")
expect_match("my name is Jatin", "small_talk")
expect_match("hi my name is Chatin", "small_talk")
expect_match("how are you doing", "small_talk")

# ---------------------------------------------------------------------------
# ─── Category J — Assistant meta & status ──────────────────────────────────
# ---------------------------------------------------------------------------

print("\n=== J — Assistant meta & status ===")

expect_match("Report version status", "report_version_status")
expect_match("System health", "report_version_status")
expect_match("system status", "report_version_status")
expect_match("what version are you running", "report_version_status")

# ---------------------------------------------------------------------------
# ─── False-positive traps ──────────────────────────────────────────────────
# ---------------------------------------------------------------------------

print("\n=== False-positive traps ===")

# Regression: open_app must resolve via the app resolver or return None (never guess)
# Using an installed app (code = Visual Studio Code) to confirm the hit:
expect_match("open code", "open_app")
# Using an app that's NOT installed to confirm the resolver correctly returns None:
expect_no_match("open firefox", "firefox not installed — resolver should return None, not guess")

# Random sentences and non-action questions should NOT match anything in Layer 0
expect_no_match("I want to eat some pizza", "random sentence")
expect_no_match("tell me a joke", "not in scope yet")
expect_no_match("play some music", "not in scope for C1")
expect_no_match("what is emotional intelligence", "general question without search keywords")

# Regression: 'how are you' must never trigger google_search
name_check, _ = match_skill("how are you")
if name_check != "google_search":
    PASS += 1
    _results.append("  ✓  'how are you' does NOT trigger google_search")
else:
    FAIL += 1
    _results.append("  ✗  'how are you' triggered google_search!")

# Regression: 'Hello my name is Jatin, how are you doing?' must route to small_talk and NEVER report_version_status
name_jatin, args_jatin = match_skill("Hello my name is Jatin, how are you doing?")
if name_jatin == "small_talk" and "Jatin" in args_jatin.get("reply", ""):
    PASS += 1
    _results.append("  ✓  'Hello my name is Jatin, how are you doing?' routes to small_talk (not report_version_status)")
else:
    FAIL += 1
    _results.append(f"  ✗  'Hello my name is Jatin, how are you doing?' misrouted to [{name_jatin}]!")

# Regression: 'List all the folders I have on desktop' must NEVER trigger open_launcher
name_desk, _ = match_skill("List all the folders I have on desktop")
if name_desk != "open_launcher":
    PASS += 1
    _results.append("  ✓  'List all the folders I have on desktop' does NOT trigger open_launcher")
else:
    FAIL += 1
    _results.append("  ✗  'List all the folders I have on desktop' triggered open_launcher!")

# Step 4 added F14 restart_service — must be THAT skill, not A12 restart.
expect_match("restart the nginx service", "restart_service", "should not match A12 restart")

# Volume 150 should be rejected (out of range), not stolen by calculator
expect_no_match("set volume to 150", "out of range")
expect_no_match("volume 150", "out of range — should not match set_volume or calculator")

# "open a book" — should not match open_app (no book app installed)
# (This may still match google_search with 'open' trigger — check)
name, args = match_skill("open a book")
_results.append(f"  info: 'open a book' → [{name}({args})]  "
                f"(acceptable: None or google_search; NOT open_app)")
if name == "open_app":
    FAIL += 1
    _results[-1] = "  ✗  " + _results[-1] + " — FAIL: should not match open_app"
else:
    PASS += 1
    _results[-1] = "  ✓  " + _results[-1]

# ---------------------------------------------------------------------------
# ─── Category M — Compound Commands Pre-Processor ──────────────────────────
# ---------------------------------------------------------------------------
print("\n=== M — Compound Commands & Multi-Action Utterances ===")
from _compound_splitter import split_compound_utterance

def test_compound(utterance: str, expected_sub_skills: list[str]) -> None:
    global PASS, FAIL
    subs = split_compound_utterance(utterance)
    matched_skills = [match_skill(s)[0] for s in subs]
    if matched_skills == expected_sub_skills:
        PASS += 1
        _results.append(f"  ✓  [compound]  {utterance!r} → {matched_skills}")
    else:
        FAIL += 1
        _results.append(f"  ✗  [compound] EXPECTED {expected_sub_skills} but got {matched_skills}  →  {utterance!r}")

test_compound("set volume to 90% and brightness to 90%", ["set_volume", "set_brightness"])
test_compound("turn off wifi then lock screen", ["toggle_wifi", "lock_screen"])
test_compound("battery status; current time", ["battery_status", "get_time"])

# Atomic query preservation checks (must not be split)
def test_atomic_preservation(utterance: str) -> None:
    global PASS, FAIL
    subs = split_compound_utterance(utterance)
    if subs == [utterance]:
        PASS += 1
        _results.append(f"  ✓  [atomic preservation]  {utterance!r} preserved as single command")
    else:
        FAIL += 1
        _results.append(f"  ✗  [atomic preservation] was mistakenly split: {subs}  →  {utterance!r}")

test_atomic_preservation("search for tom and jerry")
test_atomic_preservation("send email to test@example.com with subject Hi and message see you then")
test_atomic_preservation("add a to-do buy milk and eggs")

# ---------------------------------------------------------------------------
# ─── Category N — Multi-Provider API Cascade & Quota Tracker ───────────────
# ---------------------------------------------------------------------------
print("\n=== N — Multi-Provider API Cascade & Quota Tracker ===")
import quota_tracker
import llm_intent
from unittest.mock import patch

# Test quota tracker marking and reset
quota_tracker.reset_quota("gemini")
if not quota_tracker.is_exhausted("gemini"):
    PASS += 1
    _results.append("  ✓  quota_tracker: 'gemini' initially not exhausted")
else:
    FAIL += 1
    _results.append("  ✗  quota_tracker: 'gemini' reported exhausted after reset")

quota_tracker.mark_exhausted("gemini", "simulated 429 rate limit")
if quota_tracker.is_exhausted("gemini"):
    PASS += 1
    _results.append("  ✓  quota_tracker: 'gemini' successfully marked exhausted")
else:
    FAIL += 1
    _results.append("  ✗  quota_tracker: 'gemini' failed to mark exhausted")

quota_tracker.reset_quota("gemini")
if not quota_tracker.is_exhausted("gemini"):
    PASS += 1
    _results.append("  ✓  quota_tracker: 'gemini' successfully reset")
else:
    FAIL += 1
    _results.append("  ✗  quota_tracker: 'gemini' still exhausted after reset")

# Test failover cascade logic
quota_tracker.mark_exhausted("gemini", "simulated 429")
with patch("llm_intent._call_openai_compatible_api") as mock_groq:
    mock_groq.return_value = ("set_volume", {"percent": 75})
    with patch("llm_intent.GROQ_API_KEY", "test_key"):
        skill, args = llm_intent.resolve_intent("set the volume to 75")
        if skill == "set_volume" and args == {"percent": 75}:
            PASS += 1
            _results.append("  ✓  llm_intent cascade: bypassed exhausted Gemini and resolved via Groq")
        else:
            FAIL += 1
            _results.append(f"  ✗  llm_intent cascade failed: got ({skill}, {args})")

quota_tracker.reset_quota("gemini")

# ---------------------------------------------------------------------------
# ─── Summary ───────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

for line in _results:
    print(line)

print(f"\n{'='*60}")
print(f"Results: {PASS} passed, {FAIL} failed")
if FAIL > 0:
    print("SMOKE TEST FAILED")
    sys.exit(1)
else:
    print("SMOKE TEST PASSED ✓")
    sys.exit(0)
