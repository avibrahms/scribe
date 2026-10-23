"""Offline regressions for the French date/fraction pronunciation boundary."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from tts_text import clean_text_for_speech, speech_lang


class FrenchNumbersTest(unittest.TestCase):
    def test_dates_and_quantities_in_context(self):
        cases = {
            "Le partage n’oblige pas à donner à chacun 3/16 de chaque bien":
                "Le partage n’oblige pas à donner à chacun 3 seizièmes de chaque bien",
            "le lundi 26/10": "le lundi 26 octobre",
            "le 01/01": "le premier janvier",
            "du 26/10 au 30/10": "du 26 octobre au 30 octobre",
            "Date : 05/02/2026": "Date : 5 février 2026",
            "le 26/10/26": "le 26 octobre 2026",
            "le 29/02/2024": "le 29 février 2024",
            "le 29/02/2025": "le 29/02/2025",
            "le 3/4 du bien": "le 3 quarts du bien",
            "pour 3/4 des héritiers": "pour 3 quarts des héritiers",
            "1/2 et 2/3": "1 demi et 2 tiers",
            "3 / 16 et 3⁄16": "3 seizièmes et 3 seizièmes",
            "1/16 + 3/16 = 4/16": "1 seizième + 3 seizièmes = 4 seizièmes",
            "5/128": "5 sur 128",
            "note : 3/10": "note : 3 sur 10",
            "Disponible 24/7": "Disponible 24 heures sur 24, 7 jours sur 7",
            "Le lundi 26/10, chacun reçoit 3/16 du bien.":
                "Le lundi 26 octobre, chacun reçoit 3 seizièmes du bien.",
            "rapport26/10": "rapport26/10",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(clean_text_for_speech(source, "fr"), expected)

    def test_detection_and_other_languages(self):
        for source in ("le lundi 26/10",
                       "Le partage n’oblige pas à donner à chacun 3/16 de chaque bien"):
            self.assertEqual(speech_lang(source), "fr")
        for lang in ("en", "es", "de", "it"):
            self.assertEqual(clean_text_for_speech("3/16", lang), "3/16")

    def test_paths_and_links(self):
        self.assertNotIn("seizième", clean_text_for_speech("/tmp/3/16/file.txt", "fr"))
        self.assertEqual(clean_text_for_speech("[le 26/10](https://example.org/3/16)", "fr"),
                         "le 26 octobre")


if __name__ == "__main__":
    unittest.main()
