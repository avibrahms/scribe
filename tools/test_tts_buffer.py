"""Offline regressions for how read-aloud audio reaches the player: whole
sentences only, in order, and a slow connection raced by a fresh one."""
import asyncio
import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

_loader = importlib.machinery.SourceFileLoader(
    "edge_tts_stream", str(BIN / "edge-tts-stream"))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
helper = importlib.util.module_from_spec(_spec)
_loader.exec_module(helper)

# Stands in for the audio player: writes down each block it is given.
FAKE_PLAYER = r"""
import os, sys
with open(sys.argv[1], "wb") as out:
    while True:
        block = os.read(0, 1 << 20)
        if not block:
            break
        out.write(block)
"""

TEXT = ("First sentence, short. " + "Then a much longer second sentence "
        "that keeps going for a while. " * 12 + "And the last one.")


class SplitTest(unittest.TestCase):
    def test_nothing_is_lost_and_units_end_on_sentences(self):
        units = helper.split_units(TEXT)
        self.assertGreater(len(units), 2)
        self.assertEqual(" ".join(units), TEXT)
        for unit in units:
            self.assertRegex(unit, r"[.!?]$")
        self.assertLessEqual(len(units[0]), helper.UNIT_CHARS[0])

    def test_abbreviations_and_numbers_do_not_end_a_unit(self):
        text = ("Thomas N. Seyfried and Dr. Smith paid 3.5 dollars, e.g. for "
                "tea. " * 12)
        for unit in helper.split_units(text):
            self.assertTrue(unit.endswith("tea."), unit[-30:])

    def test_every_unit_has_something_to_say(self):
        self.assertEqual(helper.split_units("..."), [])
        self.assertEqual(helper.split_units("Hi"), ["Hi"])
        text = "Word. " * 40 + "... !!! " * 40 + "End."
        for unit in helper.split_units(text):
            self.assertRegex(unit, r"\w")

    def test_endless_sentence_is_cut_between_words(self):
        units = helper.split_units("word " * 500)
        self.assertGreater(len(units), 3)
        self.assertEqual(" ".join(units), ("word " * 500).strip())


class ReadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "audio")
        player = os.path.join(self.tmp.name, "player.py")
        with open(player, "w") as f:
            f.write(FAKE_PLAYER)
        for name in ("PLAYER_CMD", "fetch_once", "SLOW_FIRST_S",
                     "SLOW_REST_S", "LOG_FILE", "endpoints",
                     "remember_endpoint"):
            self.addCleanup(setattr, helper, name, getattr(helper, name))
        helper.PLAYER_CMD = [sys.executable, player, self.out]
        helper.SLOW_FIRST_S = 0.1
        helper.SLOW_REST_S = 0.1
        helper.LOG_FILE = os.path.join(self.tmp.name, "log")
        helper.endpoints = lambda: ["wss://one", "wss://two"]
        helper.remember_endpoint = lambda url: None
        self.calls = []

    def serve(self, behaviour):
        """behaviour(text, nth call for that text) -> seconds before the
        audio arrives, or an exception to raise."""
        async def fetch_once(text, voice, rate, pitch, volume, urls, read,
                             attempt):
            nth = sum(1 for t in self.calls if t == text)
            self.calls.append(text)
            what = behaviour(text, nth)
            if isinstance(what, Exception):
                if read["url"] == 0:
                    read["url"] = 1
                raise what
            await asyncio.sleep(what)
            attempt["first"] = time.monotonic()
            return f"<{text}>".encode()
        helper.fetch_once = fetch_once

    def read(self, pieces):
        result = helper.speak_edge(pieces, "v", "+0%", "+0Hz", "+0%")
        try:
            with open(self.out, "rb") as f:
                return result, f.read()
        except OSError:
            return result, b""

    def expected(self, pieces):
        out = b""
        for n, piece in enumerate(pieces):
            for k, unit in enumerate(helper.split_units(piece)):
                if n and k == 0:
                    out += helper.ROW_SILENCE
                out += f"<{unit}>".encode()
        return out

    def test_units_play_whole_and_in_order(self):
        # Later units arrive first: the player must still get them in order.
        units = helper.split_units(TEXT)
        self.serve(lambda text, nth: 0.3 - 0.1 * units.index(text)
                   if units.index(text) < 3 else 0)
        (spoke, errors, played), audio = self.read([TEXT])
        self.assertTrue(spoke and played)
        self.assertEqual(errors, [])
        self.assertEqual(audio, self.expected([TEXT]))

    def test_slow_connection_is_raced(self):
        # The first connection of every unit hangs; a fresh one is fast.
        self.serve(lambda text, nth: 30 if nth == 0 else 0)
        started = time.monotonic()
        (spoke, errors, played), audio = self.read([TEXT])
        self.assertTrue(spoke)
        self.assertEqual(audio, self.expected([TEXT]))
        self.assertLess(time.monotonic() - started, 5)
        with open(helper.LOG_FILE) as f:
            self.assertRegex(f.read(), r" ok .* raced=[1-9]")

    def test_failed_connection_is_retried_elsewhere(self):
        self.serve(lambda text, nth: OSError("refused") if nth == 0 else 0)
        (spoke, errors, played), audio = self.read([TEXT])
        self.assertTrue(spoke)
        self.assertTrue(errors)
        self.assertEqual(audio, self.expected([TEXT]))

    def test_service_down_plays_nothing(self):
        self.serve(lambda text, nth: OSError("refused"))
        (spoke, errors, played), audio = self.read([TEXT])
        self.assertFalse(spoke or played)
        self.assertIsInstance(errors[-1], OSError)
        self.assertEqual(audio, b"")
        with open(helper.LOG_FILE) as f:
            self.assertIn(" failed(refused) ", f.read())

    def test_table_rows_keep_their_silence(self):
        pieces = ["Table one.", "x, y.", "z, w."]
        self.serve(lambda text, nth: 0)
        (spoke, _, _), audio = self.read(pieces)
        self.assertTrue(spoke)
        self.assertEqual(audio.count(helper.ROW_SILENCE), 2)
        self.assertEqual(audio, self.expected(pieces))

    def test_missing_player_is_reported(self):
        helper.PLAYER_CMD = [os.path.join(self.tmp.name, "nope")]
        self.serve(lambda text, nth: 0)
        (spoke, errors, played), _ = self.read(["Hello there."])
        self.assertFalse(spoke)
        self.assertIsInstance(errors[-1], helper.PlayerError)


if __name__ == "__main__":
    unittest.main()
