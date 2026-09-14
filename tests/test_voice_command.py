# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""Tests for A6.2 Step 1 · Tier 0 regex router (learning/voice_command.py)."""

from __future__ import annotations

import pytest

from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.inference import FakeInferenceBackend
from neuropaca.learning.prompts import (
    VOICE_COMMAND_ACTIONS,
    VOICE_COMMAND_MAX_TOKENS,
    _tokenize_words,
    build_voice_command_grammar,
    build_voice_command_prompt,
    parse_voice_command,
)
from neuropaca.learning.voice_command import VoiceCommand, try_pattern_match


@pytest.mark.parametrize(
    ("text", "expected_action", "expected_target"),
    [
        ("open Chrome", "open", "Chrome"),
        ("Open the calculator", "open", "calculator"),
        ("open a terminal", "open", "terminal"),
        ("please open spotify", "open", "spotify"),
        ("open Files.", "open", "Files"),
        ("close Firefox", "close", "Firefox"),
        ("please close the music player", "close", "music player"),
        ("turn up the volume", "increase", "volume"),
        ("turn the volume up", "increase", "volume"),
        ("turn down the volume", "decrease", "volume"),
        ("turn the volume down", "decrease", "volume"),
        ("volume up", "increase", "volume"),
        ("volume down", "decrease", "volume"),
        ("increase the volume", "increase", "volume"),
        ("decrease the volume", "decrease", "volume"),
        ("increase the brightness", "increase", "brightness"),
        ("decrease the brightness", "decrease", "brightness"),
        ("brightness up", "increase", "brightness"),
        ("turn down the brightness!", "decrease", "brightness"),
    ],
)
def test_try_pattern_match_positive(text: str, expected_action: str, expected_target: str) -> None:
    cmd = try_pattern_match(text)
    assert cmd is not None
    assert isinstance(cmd, VoiceCommand)
    assert cmd.action == expected_action
    assert cmd.target == expected_target
    assert cmd.source == "pattern"


@pytest.mark.parametrize(
    "text",
    [
        "I opened Chrome yesterday",
        "The volume is too loud",
        "Can you open the window later",
        "It is bright outside",
        "open",
        "close",
        "please open",
        "what time is it",
        "remind me to open the document",
        "why did you close the tab",
        "",
        "   ",
        "increasingly difficult problem",
        "the brightness of this screen is high",
    ],
)
def test_try_pattern_match_negative(text: str) -> None:
    assert try_pattern_match(text) is None


# --------------------------------------------------------------------------- Step 3: Tier 1


def test_tokenize_words() -> None:
    words = _tokenize_words("Can you, please, open Google Chrome?!")
    assert words == ["Can", "you", "please", "open", "Google", "Chrome"]


def test_build_voice_command_grammar_shape() -> None:
    grammar = build_voice_command_grammar(["w1", "w2", "w3"])
    assert '\\"w1\\"' in grammar
    assert '\\"w2\\"' in grammar
    assert '\\"w3\\"' in grammar
    assert "target_start" in grammar
    assert "target_end" in grammar
    for act in VOICE_COMMAND_ACTIONS:
        assert f'\\"{act}\\"' in grammar


def test_build_voice_command_grammar_rejects_empty() -> None:
    with pytest.raises(ValueError, match="at least one word alias"):
        build_voice_command_grammar([])


def test_build_voice_command_grammar_rejects_bad_format() -> None:
    with pytest.raises(ValueError, match="not a word alias"):
        build_voice_command_grammar(["n1"])  # n1 is a node alias, not a word alias


def test_build_voice_command_grammar_rejects_duplicates() -> None:
    with pytest.raises(ValueError, match="duplicate aliases"):
        build_voice_command_grammar(["w1", "w1"])


def test_build_voice_command_prompt() -> None:
    words = ["launch", "Spotify"]
    prompt = build_voice_command_prompt("launch Spotify", words)
    assert "[w1] launch" in prompt
    assert "[w2] Spotify" in prompt
    assert 'Utterance: "launch Spotify"' in prompt


def test_parse_voice_command_valid_span() -> None:
    words = ["could", "you", "launch", "Google", "Chrome", "now"]
    raw = '{"action": "open", "target_start": "w4", "target_end": "w5"}'
    cmd = parse_voice_command(raw, words)
    assert cmd is not None
    assert cmd.action == "open"
    assert cmd.target == "Google Chrome"
    assert cmd.source == "model"


def test_parse_voice_command_no_target_word() -> None:
    words = ["make", "it", "louder"]
    raw = '{"action": "increase", "target_start": null, "target_end": null}'
    cmd = parse_voice_command(raw, words)
    assert cmd is not None
    assert cmd.action == "increase"
    assert cmd.target is None
    assert cmd.source == "model"


def test_parse_voice_command_rejects_reversed_span() -> None:
    words = ["open", "Google", "Chrome"]
    raw = '{"action": "open", "target_start": "w3", "target_end": "w2"}'
    assert parse_voice_command(raw, words) is None


def test_parse_voice_command_rejects_out_of_range_alias() -> None:
    words = ["open", "Chrome"]
    raw = '{"action": "open", "target_start": "w1", "target_end": "w99"}'
    assert parse_voice_command(raw, words) is None


def test_parse_voice_command_rejects_half_null_span() -> None:
    words = ["open", "Chrome"]
    raw = '{"action": "open", "target_start": "w2", "target_end": null}'
    assert parse_voice_command(raw, words) is None
    raw2 = '{"action": "open", "target_start": null, "target_end": "w2"}'
    assert parse_voice_command(raw2, words) is None


def test_parse_voice_command_rejects_null_action() -> None:
    words = ["hello", "world"]
    raw = '{"action": null, "target_start": null, "target_end": null}'
    assert parse_voice_command(raw, words) is None


def test_parse_voice_command_rejects_unknown_action() -> None:
    words = ["hack", "system"]
    raw = '{"action": "destroy", "target_start": "w2", "target_end": "w2"}'
    assert parse_voice_command(raw, words) is None


def test_parse_voice_command_rejects_malformed_json() -> None:
    words = ["open", "Chrome"]
    assert parse_voice_command("not json", words) is None
    assert parse_voice_command("{}", words) is None


async def test_tier1_fake_inference_integration() -> None:
    backend = FakeInferenceBackend()
    runtime = BitNetRuntime(backend, backend)
    await runtime.load_interactive_model_async()

    text = "could you launch Brave"
    words = _tokenize_words(text)
    aliases = [f"w{i + 1}" for i in range(len(words))]
    grammar = build_voice_command_grammar(aliases)
    prompt = build_voice_command_prompt(text, words)

    raw = await runtime.infer_async(
        prompt, VOICE_COMMAND_MAX_TOKENS, 0.0, grammar, interactive=True
    )
    cmd = parse_voice_command(raw, words)
    assert cmd is not None
    assert cmd.action in VOICE_COMMAND_ACTIONS
    assert cmd.source == "model"
