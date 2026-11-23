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


class ChunkTest(unittest.TestCase):
    def test_pieces_stay_small_and_end_with_a_sentence(self):
        text = " ".join(f"Sentence number {i} is here." for i in range(1, 80))
        chunks = helper.chunk_text(text)
        self.assertGreater(len(chunks), 3)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), helper.CHUNK_CHARS)
            self.assertTrue(chunk.endswith("."))
        self.assertEqual(" ".join(chunks), text)

    def test_a_long_enough_piece_ends_with_its_paragraph(self):
        para = " ".join("This sentence fills a paragraph." for _ in range(9))
        chunks = helper.chunk_text(para + "\n\nNext one. And more.")
        self.assertEqual(chunks, [para, "Next one. And more."])

    def test_short_paragraphs_are_asked_for_together(self):
        self.assertEqual(helper.chunk_text("Title\n\nA short line.\nAnother."),
                         ["Title\n\nA short line.\nAnother."])

    def test_a_very_long_sentence_is_cut_between_words(self):
        sentence = ", ".join(["a clause of several words"] * 60) + "."
        chunks = helper.chunk_text(sentence)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), helper.CHUNK_CHARS)
        for chunk in chunks[:-1]:
            self.assertTrue(chunk.endswith(","))
        self.assertEqual(" ".join(chunks), sentence)
        words = "word " * 300
        self.assertEqual(" ".join(helper.chunk_text(words)).split(),
                         words.split())


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

    def test_only_the_first_piece_keeps_its_id3_tag(self):
        tag = b"ID3\x04\x00\x00\x00\x00\x00\x05TAGGY"
        text = " ".join("This sentence fills a paragraph." for _ in range(20))
        self.assertEqual(len(helper.chunk_text(text)), 2)
        (reason, _), player, _, _ = self.run_stream(
            [io.BytesIO(tag + b"one"), io.BytesIO(tag + b"two")], text)
        self.assertIsNone(reason)
        self.assertEqual(player.stdin.getvalue(), tag + b"one" + b"two")

    def test_a_piece_without_a_tag_is_played_whole(self):
        text = " ".join("This sentence fills a paragraph." for _ in range(20))
        (_, _), player, _, _ = self.run_stream(
            [io.BytesIO(b"first piece audio"), io.BytesIO(b"second piece audio")],
            text)
        self.assertEqual(player.stdin.getvalue(),
                         b"first piece audio" + b"second piece audio")

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
