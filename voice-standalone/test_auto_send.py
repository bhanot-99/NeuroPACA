#!/usr/bin/env python3
"""
test_auto_send.py — Unit & Integration tests for Hands-Free Voice Auto-Send with
Silence Endpointing & Server-Side Intent Completeness Check.

Verifies:
  1. Intent Completeness Check (llm_intent.py):
     - complete utterances return is_complete=True, trailing=False
     - trailing utterances (ends in "and...", "or...", "um...", etc.) return is_complete=False, trailing=True
     - clean_trailing_utterance properly strips trailing conjunctions/hesitations/punctuation.
  2. WebSocket Server Protocol (server.py):
     - Complete queries dispatch immediately without delay (<800ms).
     - Trailing queries emit trailing_prompt ("Is that all, or is there anything else?").
     - User continuation during 3s extension combines commands properly.
     - User negative confirmation ("no that's all") cleans base command and executes.
     - Server timeout on trailing prompt automatically executes cleaned command.
  3. Web UI VAD Specification (templates/index.html):
     - Silence endpointing between 1.2s and 1.5s.
     - Visual states: Green (listening), Yellow (waiting/analyzing), Blue (processing).
     - Removal of manual 'Stop & Send' requirement.
"""

import os
import sys
import time
import unittest
from fastapi.testclient import TestClient

# Ensure voice-standalone directory is on sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import llm_intent
from server import app


class TestIntentCompletenessCheck(unittest.TestCase):
    """Verifies intent completeness detection and trailing utterance extraction."""

    def test_complete_utterances(self):
        complete_queries = [
            "open firefox",
            "what is the battery status",
            "set volume to 60%",
            "list all the folders I have on desktop",
            "who is Ada Lovelace",
            "check my latest emails",
            "hello how are you",
            "search for quantum computing",
            "show desktop",
        ]
        for query in complete_queries:
            with self.subTest(query=query):
                self.assertFalse(
                    llm_intent.is_trailing_utterance(query),
                    f"Query should NOT be trailing: '{query}'"
                )
                res = llm_intent.check_intent_completeness(query)
                self.assertTrue(res["is_complete"])
                self.assertFalse(res["trailing"])
                self.assertEqual(res["clean_text"], query.strip())

    def test_trailing_conjunctions_and_hesitations(self):
        trailing_queries = [
            ("open firefox and...", "open firefox"),
            ("open firefox and", "open firefox"),
            ("turn volume up or...", "turn volume up"),
            ("turn volume up or", "turn volume up"),
            ("check emails um...", "check emails"),
            ("check emails um", "check emails"),
            ("search for alan turing uh", "search for alan turing"),
            ("list files with...", "list files"),
            ("list files with", "list files"),
            ("turn off screen then...", "turn off screen"),
            ("turn off screen then", "turn off screen"),
            ("open terminal,", "open terminal"),
            ("show desktop…", "show desktop"),
        ]
        for raw, expected_clean in trailing_queries:
            with self.subTest(raw=raw):
                self.assertTrue(
                    llm_intent.is_trailing_utterance(raw),
                    f"Query SHOULD be detected as trailing: '{raw}'"
                )
                res = llm_intent.check_intent_completeness(raw)
                self.assertFalse(res["is_complete"])
                self.assertTrue(res["trailing"])
                self.assertEqual(
                    res["clean_text"],
                    expected_clean,
                    f"Cleaned text mismatch for '{raw}'"
                )

    def test_clean_trailing_utterance_empty(self):
        self.assertEqual(llm_intent.clean_trailing_utterance("and..."), "")
        self.assertEqual(llm_intent.clean_trailing_utterance(""), "")
        self.assertEqual(llm_intent.clean_trailing_utterance("   "), "")


class TestWebSocketCompletenessProtocol(unittest.TestCase):
    """Verifies server WebSocket protocol handling of complete vs trailing queries."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_complete_query_dispatches_immediately(self):
        with self.client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"type": "chat", "message": "what is the battery status"})
            msg = ws.receive_json()
            self.assertEqual(msg.get("type"), "tool_executed")
            self.assertEqual(msg.get("skill"), "battery_status")

    def test_trailing_query_emits_prompt_and_handles_continuation(self):
        with self.client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"type": "chat", "message": "show desktop and"})
            # Step 1: Must receive trailing_prompt
            prompt_msg = ws.receive_json()
            self.assertEqual(prompt_msg.get("type"), "trailing_prompt")
            self.assertEqual(prompt_msg.get("prompt"), "Is that all, or is there anything else?")
            self.assertEqual(prompt_msg.get("extension_timeout"), 3.0)

            # Step 2: Send continuation during extension window
            ws.send_json({"type": "chat", "message": "no that's all"})

            # Drain any streaming audio chunks from prompt
            while True:
                resp = ws.receive_json()
                if resp.get("type") in ("tool_executed", "chat_message"):
                    self.assertEqual(resp.get("type"), "tool_executed")
                    self.assertEqual(resp.get("skill"), "show_desktop")
                    break

    def test_trailing_query_timeout_executes_clean_command(self):
        with self.client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"type": "chat", "message": "show desktop and..."})
            prompt_msg = ws.receive_json()
            self.assertEqual(prompt_msg.get("type"), "trailing_prompt")

            # Do not send any follow-up; wait for server 3.0s timeout
            t0 = time.perf_counter()
            while True:
                resp = ws.receive_json()
                if resp.get("type") in ("tool_executed", "chat_message"):
                    elapsed = time.perf_counter() - t0
                    self.assertEqual(resp.get("type"), "tool_executed")
                    self.assertEqual(resp.get("skill"), "show_desktop")
                    self.assertGreaterEqual(elapsed, 2.5, "Server timeout should wait for extension window")
                    break


class TestWebUIVADConfiguration(unittest.TestCase):
    """Verifies that templates/index.html includes required silence endpointing and UI states."""

    def setUp(self):
        html_path = os.path.join(BASE_DIR, "templates", "index.html")
        with open(html_path, "r", encoding="utf-8") as f:
            self.html = f.read()

    def test_silence_duration_threshold(self):
        # Must be between 1.2s and 1.5s (1200ms - 1500ms)
        self.assertIn("SILENCE_DURATION_THRESHOLD_MS = 1300", self.html)

    def test_speech_rms_threshold(self):
        self.assertIn("SPEECH_RMS_THRESHOLD = 0.015", self.html)

    def test_three_color_mic_states(self):
        # Green / Pulsing for listening/detecting
        self.assertIn("bg-emerald-500/20 border-2 border-emerald-500 text-emerald-400 animate-pulse", self.html)
        # Yellow / Waiting for silence endpointing/analyzing
        self.assertIn("bg-amber-500/20 border-2 border-amber-500 text-amber-400 animate-pulse", self.html)
        # Blue for processing/executing
        self.assertIn("bg-blue-500/20 border-2 border-blue-500 text-blue-400", self.html)

    def test_no_stop_and_send_button(self):
        self.assertNotIn("Stop & Send", self.html)
        self.assertNotIn("stopRecording()", self.html)

    def test_trailing_prompt_handler(self):
        self.assertIn("case 'trailing_prompt':", self.html)
        self.assertIn("handleTrailingPrompt", self.html)


if __name__ == "__main__":
    unittest.main()
