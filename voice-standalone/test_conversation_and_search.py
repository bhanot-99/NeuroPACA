#!/usr/bin/env python3
"""
Unit tests for:
  1. Restricting web search triggers (skills/search.py) to explicit keywords.
  2. Small-talk fast-path (skills/conversation.py).
  3. Decoupling answer generation from browser launching (actions.py).
"""

import io
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure voice-standalone is on sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import actions
from skills import match_skill
from skills.conversation import _match_small_talk
from skills.search import _match_google_search


class TestExplicitSearchTriggers(unittest.TestCase):
    """Verifies google_search requires explicit keywords and rejects casual/general phrases."""

    def test_explicit_search_for(self):
        res = _match_google_search("search for python tutorials")
        self.assertIsNotNone(res)
        self.assertEqual(res["query"], "python tutorials")

    def test_explicit_google_prefix(self):
        res = _match_google_search("google the best coffee shops")
        self.assertIsNotNone(res)
        self.assertEqual(res["query"], "the best coffee shops")

    def test_explicit_search_web(self):
        res = _match_google_search("search the web for Linux tips")
        self.assertIsNotNone(res)
        self.assertEqual(res["query"], "Linux tips")

    def test_explicit_look_up_on_google(self):
        res = _match_google_search("look up quantum physics on google")
        self.assertIsNotNone(res)
        self.assertEqual(res["query"], "quantum physics")

    def test_rejects_casual_conversation(self):
        self.assertIsNone(_match_google_search("how are you"))
        self.assertIsNone(_match_google_search("hello"))
        self.assertIsNone(_match_google_search("who are you"))
        self.assertIsNone(_match_google_search("good morning"))
        self.assertIsNone(_match_google_search("what's up"))

    def test_rejects_general_questions_without_search_keywords(self):
        self.assertIsNone(_match_google_search("what is emotional intelligence"))
        self.assertIsNone(_match_google_search("tell me about gravity"))
        self.assertIsNone(_match_google_search("why is the sky blue"))
        self.assertIsNone(_match_google_search("I want to eat pizza"))


class TestSmallTalkFastPath(unittest.TestCase):
    """Verifies greetings and conversational inputs resolve to small_talk."""

    def test_how_are_you(self):
        res = _match_small_talk("how are you")
        self.assertIsNotNone(res)
        self.assertIn("doing well", res["reply"])

        res2 = _match_small_talk("hello how are you")
        self.assertIsNotNone(res2)
        self.assertIn("doing well", res2["reply"])

        res3 = _match_small_talk("how's it going")
        self.assertIsNotNone(res3)
        self.assertIn("doing well", res3["reply"])

    def test_greetings(self):
        for greeting in ["hello", "hi", "hey", "good morning", "good afternoon", "good evening", "what's up"]:
            res = _match_small_talk(greeting)
            self.assertIsNotNone(res, f"Failed to match greeting: {greeting}")
            self.assertTrue(len(res["reply"]) > 0)

    def test_identity(self):
        res = _match_small_talk("who are you")
        self.assertIsNotNone(res)
        self.assertIn("NeuroPaca", res["reply"])

        res2 = _match_small_talk("what is your name")
        self.assertIsNotNone(res2)
        self.assertIn("NeuroPaca", res2["reply"])

    def test_layer0_match_skill_integration(self):
        name, args = match_skill("how are you")
        self.assertEqual(name, "small_talk")
        self.assertIsNotNone(args)
        self.assertIn("reply", args)

        name2, _ = match_skill("hello")
        self.assertEqual(name2, "small_talk")

        name3, _ = match_skill("who are you")
        self.assertEqual(name3, "small_talk")

    def test_greeting_with_name_and_how_are_you(self):
        from skills._session_state import set_user_name, get_user_name
        set_user_name(None)
        name, args = match_skill("Hello my name is Jatin, how are you doing?")
        self.assertEqual(name, "small_talk")
        self.assertNotEqual(name, "report_version_status")
        self.assertIsNotNone(args)
        self.assertIn("Jatin", args.get("reply", ""))
        self.assertIn("doing well", args.get("reply", ""))
        self.assertEqual(get_user_name(), "Jatin")

    def test_report_version_status_and_system_health(self):
        name1, _ = match_skill("Report version status")
        self.assertEqual(name1, "report_version_status")

        name2, _ = match_skill("System health")
        self.assertEqual(name2, "report_version_status")

        name3, _ = match_skill("what version are you running")
        self.assertEqual(name3, "report_version_status")

        name4, _ = match_skill("system status")
        self.assertEqual(name4, "report_version_status")

    def test_non_conversational_sentences_not_matched(self):
        self.assertIsNone(_match_small_talk("open visual studio code"))
        self.assertIsNone(_match_small_talk("set volume to 60"))
        self.assertIsNone(_match_small_talk("search for cats on youtube"))


class TestDecoupledAnswerQuestion(unittest.TestCase):
    """Verifies answer_question speaks answers directly and only launches browser on explicit search words."""

    @patch("actions._open_url")
    def test_general_qa_does_not_open_browser(self, mock_open_url):
        question = "what is emotional intelligence"
        answer = "Emotional intelligence is the capacity to be aware of and express one's emotions."

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            actions.answer_question(question, answer)

        # Output must contain the spoken answer
        self.assertIn("[answer] Emotional intelligence is", out.getvalue())
        # Browser must NOT be opened
        mock_open_url.assert_not_called()

    @patch("actions._open_url")
    def test_explicit_search_command_opens_browser(self, mock_open_url):
        question = "search for emotional intelligence"
        answer = "Emotional intelligence refers to the ability to understand emotions."

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            actions.answer_question(question, answer)

        # Output must contain the answer
        self.assertIn("[answer] Emotional intelligence refers to", out.getvalue())
        # Browser MUST be opened because 'search' was in the command
        mock_open_url.assert_called_once()
        url_called = mock_open_url.call_args[0][0]
        self.assertIn("search?q=", url_called)

    @patch("actions._open_url")
    def test_explicit_google_command_opens_browser(self, mock_open_url):
        question = "google emotional intelligence"
        answer = "Emotional intelligence is awareness of emotions."

        with patch("sys.stdout", new_callable=io.StringIO):
            actions.answer_question(question, answer)

        mock_open_url.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
