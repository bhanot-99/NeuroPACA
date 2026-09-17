"""
test_compound_splitter.py — Unit and Regression Tests for Compound Splitter.

Tests action-verb boundary detection and atomic guard scoping in _compound_splitter.py.
"""

import unittest
from _compound_splitter import (
    split_compound_utterance,
    _is_atomic_command,
    ACTION_VERBS,
)


class TestCompoundSplitter(unittest.TestCase):
    def test_action_verbs_constant(self):
        """Verify ACTION_VERBS regex constant is defined and matches required verbs."""
        expected_verbs = [
            "send", "set", "read", "open", "lock", "turn", "show",
            "calculate", "list", "create", "delete", "run", "launch",
            "fetch", "check",
        ]
        import re
        pat = re.compile(ACTION_VERBS, re.IGNORECASE)
        for verb in expected_verbs:
            self.assertTrue(pat.search(verb), f"Verb {verb!r} should match ACTION_VERBS pattern")

    def test_search_and_send_split(self):
        """Primary regression test: 'Search for Tom and Jerry and send an email to Alice'."""
        utterance = "Search for Tom and Jerry and send an email to Alice"
        expected = ["Search for Tom and Jerry", "send an email to Alice"]
        self.assertEqual(split_compound_utterance(utterance), expected)

    def test_search_and_send_with_comma_and_then(self):
        """Verify compound split with comma conjunction variations."""
        utterance = "Search for Tom and Jerry, and then send an email to Alice"
        expected = ["Search for Tom and Jerry", "send an email to Alice"]
        self.assertEqual(split_compound_utterance(utterance), expected)

    def test_find_prefix_guard_scoping(self):
        """Verify 'find' atomic guard scopes properly at action-verb boundaries."""
        utterance = "find Tom and Jerry and send an email to Alice"
        expected = ["find Tom and Jerry", "send an email to Alice"]
        self.assertEqual(split_compound_utterance(utterance), expected)

        preserved = "find Tom and Jerry"
        self.assertEqual(split_compound_utterance(preserved), [preserved])

    def test_atomic_preservation_queries(self):
        """Utterances with atomic prefix guards and no command verbs must be preserved whole."""
        cases = [
            "search for tom and jerry",
            "search for peanut butter and jelly",
            "find Romeo and Juliet",
            "google rock and roll",
            "send email to test@example.com with subject Hi and message see you then",
            "add a to-do buy milk and eggs",
            "calculate 10 + 20 and 30",
        ]
        for utterance in cases:
            self.assertEqual(
                split_compound_utterance(utterance),
                [utterance],
                f"Utterance {utterance!r} was mistakenly split",
            )

    def test_standard_compound_commands(self):
        """Non-atomic commands should split cleanly on conjunctions."""
        self.assertEqual(
            split_compound_utterance("set volume to 90% and brightness to 90%"),
            ["set volume to 90%", "brightness to 90%"],
        )
        self.assertEqual(
            split_compound_utterance("turn off wifi then lock screen"),
            ["turn off wifi", "lock screen"],
        )
        self.assertEqual(
            split_compound_utterance("battery status; current time"),
            ["battery status", "current time"],
        )

    def test_chained_multi_action_split(self):
        """Utterances chaining atomic search prefix and multiple action verbs."""
        utterance = "Search for Tom and Jerry and send an email to Alice and lock screen"
        expected = [
            "Search for Tom and Jerry",
            "send an email to Alice",
            "lock screen",
        ]
        self.assertEqual(split_compound_utterance(utterance), expected)

    def test_atomic_prefix_followed_by_secondary_action(self):
        """Verify other atomic prefixes terminate at secondary action conjunctions."""
        self.assertEqual(
            split_compound_utterance("add a to-do buy milk and turn off wifi"),
            ["add a to-do buy milk", "turn off wifi"],
        )
        self.assertEqual(
            split_compound_utterance("search for python tutorials and calculate 42 times 7"),
            ["search for python tutorials", "calculate 42 times 7"],
        )
        self.assertEqual(
            split_compound_utterance(
                "send email to test@example.com with subject Hi and message see you then and lock screen"
            ),
            [
                "send email to test@example.com with subject Hi and message see you then",
                "lock screen",
            ],
        )

    def test_quoted_string_protection(self):
        """Conjunctions inside quotes must be preserved."""
        utterance = 'search for "cats and dogs" and send an email to Alice'
        expected = ['search for "cats and dogs"', 'send an email to Alice']
        self.assertEqual(split_compound_utterance(utterance), expected)

    def test_is_atomic_command_scoping(self):
        """_is_atomic_command must return False when secondary action verbs are present."""
        self.assertTrue(_is_atomic_command("search for tom and jerry"))
        self.assertTrue(_is_atomic_command("send email to a@b.com with subject Hi and message bye"))
        self.assertFalse(_is_atomic_command("Search for Tom and Jerry and send an email to Alice"))
        self.assertFalse(_is_atomic_command("search for cats; lock screen"))
        self.assertFalse(_is_atomic_command("set volume to 90% and brightness to 90%"))


if __name__ == "__main__":
    unittest.main()
