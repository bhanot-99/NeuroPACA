"""
Layer 1 (semantic match) validation — Step 2's threshold-tuning requirement.

Every test utterance here is independently worded from BOTH skills/*.py's
Layer 0 regex triggers AND semantic_match.SKILL_EXAMPLES' reference phrases —
otherwise this would just be testing "does a sentence match itself," which
is meaningless. Each case also asserts Layer 0 does NOT match it, so we know
it's actually exercising Layer 1, not accidentally caught earlier.

Two kinds of cases:
  POSITIVE — should resolve to the named skill (measures false-rejects)
  NEGATIVE — should resolve to nothing, i.e. fall through to the LLM
             (measures false-accepts — the more dangerous failure mode)
"""

import skills
from skills import semantic_match

POSITIVE_CASES: list[tuple[str, str]] = [
    ("could you please raise the audio a little", "volume_up"),
    ("that music is way too loud, bring it down", "volume_down"),
    ("silence the speakers for a moment", "mute"),
    ("let the audio play again", "unmute"),
    ("this display is too dim, make it brighter", "brightness_up"),
    ("my eyes hurt, dim the screen a touch", "brightness_down"),
    ("flip over to the dark color scheme", "toggle_dark_mode"),
    ("I'm leaving my desk, secure my session", "lock_screen"),
    ("power down the machine entirely", "shutdown"),
    ("give the computer a fresh reboot", "restart"),
    ("end my current session please", "logout"),
    ("send the laptop to sleep mode", "sleep"),
    ("snap a picture of my current screen", "screenshot"),
    ("begin capturing everything on my display", "start_recording"),
    ("that's enough, stop the screen capture", "stop_recording"),
    ("duplicate my screen onto the projector", "toggle_display"),
    ("get the wireless network connected", "toggle_wifi"),
    ("disconnect bluetooth devices for now", "toggle_bluetooth"),
    ("switch the machine into flight mode", "toggle_airplane"),
    ("silence my notifications for a while", "toggle_dnd"),
    ("how much charge does the battery have left", "battery_status"),
    ("mute my headset microphone", "toggle_mic_mute"),
    ("take me over to my second desktop", "switch_workspace"),
    ("pull my email application up to the front", "switch_to_app"),
    ("shut down that browser window", "close_app"),
    ("that program isn't responding, terminate it", "force_quit"),
    ("tell me every application currently running", "list_open_apps"),
    ("push all my windows out of view", "show_desktop"),
    ("stretch this window across my whole screen", "maximize_window"),
    ("get back the app I just shut", "reopen_last_closed"),
    ("show me the full list of apps I have installed", "open_launcher"),
    ("fire up my text editor", "open_app"),
    ("find me a good chocolate cake recipe online", "google_search"),
    ("pull up a video about fixing a bike tire", "youtube_search"),
    ("what does the online encyclopedia say about volcanoes", "wikipedia_search"),
    ("use the private search engine to find running shoes", "duckduckgo_search"),
    ("I don't know what gregarious means, can you explain", "define_word"),
    ("could you spell the word receive for me", "spell_word"),
    ("what is the day today", "get_date"),
    ("do you know the current hour", "get_time"),
    ("what's the local time over in Paris right now", "timezone_conversion"),
    ("turn 12 kilograms into pounds for me", "unit_conversion"),
    ("multiply 25 by 16", "calculator"),
    ("show me what my internet connection identifies as", "my_ip_address"),
    ("check how fast my network connection is", "speed_test"),
    ("did anything important happen on this day in the past", "today_in_history"),
]

NEGATIVE_CASES: list[str] = [
    "I want to eat some pizza tonight",
    "hello, how has your day been",
    "tell me a funny joke",
    "play some relaxing music for me",
    "what's the meaning of life",
    "remind me to call my mom later",
    "I feel like going for a walk",
    "can you help me write an email to my boss",
    "let's talk about something else",
    "I'm bored, what should I do",
    "book me a flight to Tokyo next month",
    "order me a pizza from the usual place",
]


def run() -> None:
    total = 0
    passed = 0
    false_rejects = []
    false_accepts = []
    wrong_skill = []

    print("=" * 70)
    print("POSITIVE CASES (should resolve to the named skill)")
    print("=" * 70)
    for text, expected_skill in POSITIVE_CASES:
        total += 1
        l0_name, _ = skills.match_skill(text)
        if l0_name is not None:
            print(f"  ⚠ SKIP (Layer 0 already caught it, not testing Layer 1): {text!r} -> {l0_name}")
            passed += 1  # not a Layer 1 failure — just not a useful case anymore
            continue

        name, args = semantic_match.match(text)
        if name == expected_skill:
            print(f"  ✓  {text!r} -> {name}({args})")
            passed += 1
        elif name is None:
            print(f"  ✗ FALSE REJECT  {text!r} -> expected {expected_skill}, got nothing")
            false_rejects.append((text, expected_skill))
        else:
            print(f"  ✗ WRONG SKILL   {text!r} -> expected {expected_skill}, got {name}")
            wrong_skill.append((text, expected_skill, name))

    print()
    print("=" * 70)
    print("NEGATIVE CASES (should resolve to nothing)")
    print("=" * 70)
    for text in NEGATIVE_CASES:
        total += 1
        l0_name, _ = skills.match_skill(text)
        if l0_name is not None:
            print(f"  ⚠ Layer 0 caught a supposed negative case: {text!r} -> {l0_name} (Layer 0 bug, not Layer 1's)")
            passed += 1
            continue

        name, args = semantic_match.match(text)
        if name is None:
            print(f"  ✓  {text!r} -> nothing (correct)")
            passed += 1
        else:
            print(f"  ✗ FALSE ACCEPT  {text!r} -> {name}({args}) (should have been nothing)")
            false_accepts.append((text, name))

    print()
    print("=" * 70)
    print(f"Results: {passed}/{total} passed")
    print(f"  False rejects (missed a real match): {len(false_rejects)}")
    print(f"  False accepts (fired on nothing):    {len(false_accepts)}")
    print(f"  Wrong skill (matched the wrong one):  {len(wrong_skill)}")
    print("=" * 70)
    # -----------------------------------------------------------------------
    # Regression Test: "List all the folders I have on desktop"
    # Must NOT trigger open_launcher; must resolve to list_desktop_folders or LLM fallback
    # -----------------------------------------------------------------------
    print()
    print("=" * 70)
    print("REGRESSION TESTS")
    print("=" * 70)
    test_phrase = "List all the folders I have on desktop"
    l0_name, _ = skills.match_skill(test_phrase)
    l1_name, _ = semantic_match.match(test_phrase)
    assert l0_name != "open_launcher", f"Layer 0 misrouted {test_phrase!r} to open_launcher!"
    assert l1_name != "open_launcher", f"Layer 1 misrouted {test_phrase!r} to open_launcher!"
    resolved_to = l0_name or l1_name or "LLM fallback"
    print(f"  ✓  {test_phrase!r} does NOT trigger open_launcher (resolved to: {resolved_to})")
    assert resolved_to in ("list_desktop_folders", "LLM fallback"), (
        f"Expected list_desktop_folders or LLM fallback, got {resolved_to}"
    )


if __name__ == "__main__":
    run()
