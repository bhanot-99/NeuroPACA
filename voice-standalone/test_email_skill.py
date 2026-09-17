#!/usr/bin/env python3
"""
Unit and Integration Tests for Category H Email Skill Overhaul:
  - skills/communication.py:
      * Parameter extraction (count, sender, query, index, full_body)
      * Ordinal and numeric index parsing ("2nd recent email", "email 3")
      * Send email regex parsing
  - actions.py:
      * Live IMAP sync behavior (INBOX selection, BODY.PEEK[], readonly=True)
      * Deleted message filtering (\\Deleted flag detection and exclusion)
      * Sender filtering (FROM search and in-memory verification)
      * Zero-match messaging ("No emails found from '...'")
      * Specific index reading (extracting Subject and full Body content)
      * Rich list formatting (Sender, Date, Subject, 100-character snippet)
"""

import email.message
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
from skills.communication import _match_read_latest_emails, _match_send_email, _parse_ordinal


def _create_mock_mime_email(sender: str, to: str, subject: str, date: str, body: str, is_html: bool = False) -> bytes:
    """Helper to generate RFC-822 raw email bytes."""
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = date
    if is_html:
        msg.set_content(body, subtype="html")
    else:
        msg.set_content(body)
    return msg.as_bytes()


class TestEmailSkillParsing(unittest.TestCase):
    """Tests parameter extraction and utterance matching in skills/communication.py."""

    def test_parse_ordinal(self):
        self.assertEqual(_parse_ordinal("first"), 1)
        self.assertEqual(_parse_ordinal("1st"), 1)
        self.assertEqual(_parse_ordinal("2nd"), 2)
        self.assertEqual(_parse_ordinal("second"), 2)
        self.assertEqual(_parse_ordinal("3rd"), 3)
        self.assertEqual(_parse_ordinal("third"), 3)
        self.assertEqual(_parse_ordinal("5th"), 5)
        self.assertEqual(_parse_ordinal("latest"), 1)
        self.assertEqual(_parse_ordinal("10"), 10)
        self.assertIsNone(_parse_ordinal("invalid"))

    def test_match_read_latest_general_list(self):
        res = _match_read_latest_emails("check my latest emails")
        self.assertIsNotNone(res)
        self.assertEqual(res.get("count"), 5)

        res = _match_read_latest_emails("show 3 emails")
        self.assertIsNotNone(res)
        self.assertEqual(res.get("count"), 3)

        res = _match_read_latest_emails("what are my recent emails")
        self.assertIsNotNone(res)
        self.assertEqual(res.get("count"), 5)

    def test_match_read_latest_sender_filter(self):
        res = _match_read_latest_emails("any email from swatik")
        self.assertIsNotNone(res)
        self.assertEqual(res.get("sender"), "swatik")
        self.assertEqual(res.get("count"), 5)

        res = _match_read_latest_emails("check emails from groww")
        self.assertIsNotNone(res)
        self.assertEqual(res.get("sender"), "groww")

        res = _match_read_latest_emails("did I get an email from boss")
        self.assertIsNotNone(res)
        self.assertEqual(res.get("sender"), "boss")

    def test_match_read_latest_query_search(self):
        res = _match_read_latest_emails("search emails for IPO")
        self.assertIsNotNone(res)
        self.assertEqual(res.get("query"), "IPO")

        res = _match_read_latest_emails("find emails about invoice")
        self.assertIsNotNone(res)
        self.assertEqual(res.get("query"), "invoice")

    def test_match_read_specific_index(self):
        # Ordinal phrasing
        res = _match_read_latest_emails("read 2nd recent email")
        self.assertIsNotNone(res)
        self.assertEqual(res.get("index"), 2)
        self.assertTrue(res.get("full_body"))

        res = _match_read_latest_emails("read the first email")
        self.assertIsNotNone(res)
        self.assertEqual(res.get("index"), 1)
        self.assertTrue(res.get("full_body"))

        # Ordinal with sender
        res = _match_read_latest_emails("read the first email from swatik")
        self.assertIsNotNone(res)
        self.assertEqual(res.get("index"), 1)
        self.assertEqual(res.get("sender"), "swatik")
        self.assertTrue(res.get("full_body"))

        # Numeric phrasing
        res = _match_read_latest_emails("read email 3")
        self.assertIsNotNone(res)
        self.assertEqual(res.get("index"), 3)
        self.assertTrue(res.get("full_body"))

        res = _match_read_latest_emails("read email number 4 from groww")
        self.assertIsNotNone(res)
        self.assertEqual(res.get("index"), 4)
        self.assertEqual(res.get("sender"), "groww")
        self.assertTrue(res.get("full_body"))

    def test_match_send_email(self):
        res = _match_send_email("send email to test@example.com with subject Hello and message How are you?")
        self.assertIsNotNone(res)
        self.assertEqual(res.get("to"), "test@example.com")
        self.assertEqual(res.get("subject"), "Hello")
        self.assertEqual(res.get("body"), "How are you?")

        # Defaults when subject or body omitted
        res = _match_send_email("email to dev@neuro.paca")
        self.assertIsNotNone(res)
        self.assertEqual(res.get("to"), "dev@neuro.paca")
        self.assertTrue(len(res.get("subject")) > 0)


class TestEmailActionsLiveSync(unittest.TestCase):
    """Tests live IMAP sync, deleted message filtering, rich formatting, and sender search."""

    def setUp(self):
        self.mock_imap = MagicMock()
        self.mock_imap.__enter__.return_value = self.mock_imap
        self.mock_imap.__exit__.return_value = None
        # Mock credentials
        self.cred_patch = patch("actions._get_mail_credentials", return_value=("testuser@gmail.com", "app_pwd_123"))
        self.cred_patch.start()
        self.imap_patch = patch("imaplib.IMAP4_SSL", return_value=self.mock_imap)
        self.imap_patch.start()

        # Build raw test email messages
        self.email1_bytes = _create_mock_mime_email(
            sender="Swati Sharma <swatik@example.com>",
            to="testuser@gmail.com",
            subject="Project Sync & Updates",
            date="Wed, 17 Sep 2026 10:15:00 +0000",
            body="Hey Jatin,\nHere is the full project summary for this week.\nAll milestones met.",
        )
        self.email2_bytes = _create_mock_mime_email(
            sender="Groww <noreply@groww.in>",
            to="testuser@gmail.com",
            subject="National Stock Exchange (NSE) IPO is live",
            date="Wed, 17 Sep 2026 09:30:00 +0000",
            body="<p>India's largest stock exchange is going public &amp; applications are open &nbsp; today!</p>",
            is_html=True,
        )
        self.email3_deleted_bytes = _create_mock_mime_email(
            sender="Spammer <spam@junk.com>",
            to="testuser@gmail.com",
            subject="Deleted Phishing Scam",
            date="Wed, 17 Sep 2026 08:00:00 +0000",
            body="You won a prize! Click here.",
        )

    def tearDown(self):
        self.cred_patch.stop()
        self.imap_patch.stop()

    def test_live_sync_imap_parameters(self):
        """Verifies IMAP selects INBOX in readonly mode and searches UNDELETED."""
        self.mock_imap.login.return_value = ("OK", [b"Success"])
        self.mock_imap.select.return_value = ("OK", [b"1"])
        self.mock_imap.search.return_value = ("OK", [b"101"])
        self.mock_imap.fetch.return_value = (
            "OK",
            [(b"101 (FLAGS () BODY[] {" + str(len(self.email1_bytes)).encode() + b"}", self.email1_bytes)],
        )

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            actions.read_latest_emails(count=1)

        # Must select INBOX in readonly mode to prevent marking unseen emails as seen
        self.mock_imap.select.assert_called_with("INBOX", readonly=True)
        # Search call must search UNDELETED
        self.assertTrue(self.mock_imap.search.called)
        search_args = self.mock_imap.search.call_args[0]
        self.assertIn("(UNDELETED)", search_args[1])
        output = out.getvalue()
        self.assertIn("Found 1 email(s)", output)
        self.assertIn("Swati Sharma <swatik@example.com>", output)

    def test_deleted_messages_strictly_filtered_out(self):
        """Verifies that messages flagged with \\Deleted are ignored and not displayed."""
        self.mock_imap.login.return_value = ("OK", [b"Success"])
        self.mock_imap.select.return_value = ("OK", [b"2"])
        # Return 2 message IDs: 101 (valid) and 102 (marked \\Deleted)
        self.mock_imap.search.return_value = ("OK", [b"101 102"])

        def mock_fetch(msg_id, query):
            if msg_id == b"101":
                return (
                    "OK",
                    [(b"101 (FLAGS (\\Seen) BODY[] {" + str(len(self.email1_bytes)).encode() + b"}", self.email1_bytes)],
                )
            else:
                return (
                    "OK",
                    [(b"102 (FLAGS (\\Deleted \\Seen) BODY[] {" + str(len(self.email3_deleted_bytes)).encode() + b"}", self.email3_deleted_bytes)],
                )

        self.mock_imap.fetch.side_effect = mock_fetch

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            actions.read_latest_emails(count=5)

        output = out.getvalue()
        # Email 101 must be present
        self.assertIn("Swati Sharma <swatik@example.com>", output)
        self.assertIn("Project Sync & Updates", output)
        # Email 102 with \\Deleted MUST NOT be present
        self.assertNotIn("Spammer", output)
        self.assertNotIn("Deleted Phishing Scam", output)

    def test_sender_search_matching(self):
        """Verifies searching with sender='Groww' passes FROM Groww and displays matching email."""
        self.mock_imap.login.return_value = ("OK", [b"Success"])
        self.mock_imap.select.return_value = ("OK", [b"1"])
        self.mock_imap.search.return_value = ("OK", [b"202"])
        self.mock_imap.fetch.return_value = (
            "OK",
            [(b"202 (FLAGS (\\Seen) BODY[] {" + str(len(self.email2_bytes)).encode() + b"}", self.email2_bytes)],
        )

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            actions.read_latest_emails(sender="Groww")

        search_args = self.mock_imap.search.call_args[0]
        self.assertIn('FROM "Groww"', search_args)
        output = out.getvalue()
        self.assertIn("Found 1 email(s) from 'Groww'", output)
        self.assertIn("National Stock Exchange (NSE) IPO is live", output)
        # Verify HTML entities (&amp;, &nbsp;) were unescaped in the snippet
        self.assertIn("&", output)
        self.assertNotIn("&amp;", output)
        self.assertNotIn("&nbsp;", output)

    def test_sender_search_zero_matches(self):
        """Verifies searching for an absent sender returns clean notification."""
        self.mock_imap.login.return_value = ("OK", [b"Success"])
        self.mock_imap.select.return_value = ("OK", [b"0"])
        self.mock_imap.search.return_value = ("OK", [b""])

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            actions.read_latest_emails(sender="nonexistent_sender")

        output = out.getvalue()
        self.assertIn("[email] No emails found from 'nonexistent_sender'.", output)

    def test_read_specific_email_index_full_body(self):
        """Verifies index=2 extracts the 2nd email and outputs full Subject and Body."""
        self.mock_imap.login.return_value = ("OK", [b"Success"])
        self.mock_imap.select.return_value = ("OK", [b"2"])
        self.mock_imap.search.return_value = ("OK", [b"101 102"])

        # Latest first: 102 is latest (email2), 101 is 2nd latest (email1)
        def mock_fetch(msg_id, query):
            if msg_id == b"102":
                return (
                    "OK",
                    [(b"102 (FLAGS (\\Seen) BODY[] {" + str(len(self.email2_bytes)).encode() + b"}", self.email2_bytes)],
                )
            else:
                return (
                    "OK",
                    [(b"101 (FLAGS (\\Seen) BODY[] {" + str(len(self.email1_bytes)).encode() + b"}", self.email1_bytes)],
                )

        self.mock_imap.fetch.side_effect = mock_fetch

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            actions.read_latest_emails(index=2)

        output = out.getvalue()
        self.assertIn("[email] Email #2 of 2:", output)
        self.assertIn("From: Swati Sharma <swatik@example.com>", output)
        self.assertIn("Subject: Project Sync & Updates", output)
        self.assertIn("Body:\nHey Jatin, Here is the full project summary for this week. All milestones met.", output)

    def test_read_invalid_index(self):
        """Verifies requesting an out-of-range index produces helpful error message."""
        self.mock_imap.login.return_value = ("OK", [b"Success"])
        self.mock_imap.select.return_value = ("OK", [b"1"])
        self.mock_imap.search.return_value = ("OK", [b"101"])
        self.mock_imap.fetch.return_value = (
            "OK",
            [(b"101 (FLAGS () BODY[] {" + str(len(self.email1_bytes)).encode() + b"}", self.email1_bytes)],
        )

        with patch("sys.stdout", new_callable=io.StringIO) as out:
            actions.read_latest_emails(index=5)

        output = out.getvalue()
        self.assertIn("[email] Invalid email index #5. Only 1 matching email(s) found.", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
