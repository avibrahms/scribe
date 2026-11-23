"""Offline regressions for the Speechify helper: what it sends, and that a
refused or rate-limited request ends the way read-aloud expects."""
import importlib.machinery
import importlib.util
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
_loader = importlib.machinery.SourceFileLoader(
    "speechify_tts_stream", str(BIN / "speechify-tts-stream"))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
helper = importlib.util.module_from_spec(_spec)
_loader.exec_module(helper)


def http_error(code, body, headers=None):
    return urllib.error.HTTPError(
        helper.API_URL, code, "", headers or {}, io.BytesIO(body.encode()))


class FakePlayer:
    def __init__(self):
        self.stdin = io.BytesIO()
        self.stdin.close = lambda: None
        self.returncode = 0

    def wait(self):
        return 0

    def poll(self):
        return 0


class RequestTest(unittest.TestCase):
    def test_text_is_always_sent_as_escaped_ssml(self):
        self.assertEqual(helper.to_ssml("a < b & \"c\"", 1.0),
                         "<speak>a &lt; b &amp; &quot;c&quot;</speak>")

    def test_speed_becomes_a_prosody_rate(self):
        self.assertEqual(helper.to_ssml("Hi.", 1.25),
                         '<speak><prosody rate="+25%">Hi.</prosody></speak>')
        self.assertEqual(helper.to_ssml("Hi.", 0.75),
                         '<speak><prosody rate="-25%">Hi.</prosody></speak>')

    def test_request_body_and_headers(self):
        req = helper.build_request("Bonjour.", "harper_32", 1.0, "fr-FR", "k")
        body = json.loads(req.data)
        self.assertEqual(req.full_url, "https://api.speechify.ai/v1/audio/stream")
        self.assertEqual(body["model"], "simba-3.5")
        self.assertEqual(body["voice_id"], "harper_32")
        self.assertEqual(body["language"], "fr-FR")
        self.assertEqual(req.get_header("Authorization"), "Bearer k")
        self.assertEqual(req.get_header("Accept"), "audio/mpeg")
        self.assertNotIn("language", json.loads(
            helper.build_request("Hi.", "harper_32", 1.0, None, "k").data))

    def test_language_is_named_only_when_the_voice_reads_another_one(self):
        settings = {"speechify_voice_langs": {"harper_32": "en"}}
        self.assertIsNone(helper.request_language("en", "harper_32", settings))
        self.assertEqual(
            helper.request_language("fr", "harper_32", settings), "fr-FR")
        self.assertIsNone(helper.request_language(None, "harper_32", settings))


class StreamTest(unittest.TestCase):
    def run_stream(self, responses, text="One. Two."):
        player = FakePlayer()
        with mock.patch.object(helper.urllib.request, "urlopen",
                               side_effect=responses) as urlopen, \
             mock.patch.object(helper.subprocess, "Popen", return_value=player), \
             mock.patch.object(helper.time, "sleep") as sleep:
            result = helper.stream_tts(text, "harper_32", 1.0, None, "k")
        return result, player, urlopen, sleep

    def test_audio_reaches_the_player(self):
        (reason, unread), player, _, _ = self.run_stream([io.BytesIO(b"mp3")])
        self.assertEqual((reason, unread), (None, ""))
        self.assertEqual(player.stdin.getvalue(), b"mp3")

    def test_used_up_allowance_hands_back_the_whole_text(self):
        body = json.dumps({"error": {"code": "payment_required",
                                     "message": "Insufficient balance"}})
        (reason, unread), _, _, _ = self.run_stream([http_error(402, body)])
        self.assertEqual(reason, "HTTP 402: Insufficient balance")
        self.assertEqual(unread, "One. Two.")

    def test_rate_limit_is_waited_out_then_read(self):
        (reason, _), player, urlopen, sleep = self.run_stream(
            [http_error(429, "{}", {"Retry-After": "2"}), io.BytesIO(b"mp3")])
        self.assertIsNone(reason)
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(2.0)
        self.assertEqual(player.stdin.getvalue(), b"mp3")

    def test_rate_limit_that_lasts_gives_up(self):
        (reason, unread), _, urlopen, _ = self.run_stream(
            [http_error(429, "{}") for _ in range(5)])
        self.assertTrue(reason.startswith("HTTP 429"))
        self.assertEqual(urlopen.call_count, helper.RATE_LIMIT_RETRIES + 1)
        self.assertEqual(unread, "One. Two.")


if __name__ == "__main__":
    unittest.main()
