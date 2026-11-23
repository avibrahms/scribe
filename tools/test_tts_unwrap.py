"""Offline regressions for joining lines cut by a narrow page (PDF text)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from tts_text import clean_text_for_speech

PARAGRAPH = (
    "Speaker: Thomas N. Seyfried, biology professor at Boston College. Channel: \"Talking Cancer With\n"
    "Professor Thomas Seyfried\".\n"
    "His strongest statements: cancer is a disease of the mitochondria (the cell's power plants), not of genes;\n"
    "tumour cells cannot burn ketones or fat; the same plan should work for most cancers; death rates will fall\n"
    "sharply once doctors accept this. It proposes trials that delay radiation and chemotherapy by up to\n"
    "6 weeks, and admits this. Ketogenic diet raised mean survival 57%, and 78% with high-pressure oxygen\n"
    "(2013). Diet plus the drug reversed symptoms in late-stage brain tumours.")


class UnwrapTest(unittest.TestCase):
    def test_wrapped_paragraph_becomes_sentences(self):
        out = clean_text_for_speech(PARAGRAPH)
        self.assertEqual(out.count("\n"), 1)
        self.assertIn("Cancer With Professor Thomas", out)
        self.assertIn("genes; tumour cells", out)
        self.assertIn("up to 6 weeks", out)
        self.assertIn("oxygen (2013)", out)

    def test_other_line_separators(self):
        self.assertEqual(
            clean_text_for_speech("one line that goes on and on\rand ends here."),
            "one line\nthat goes on and on and ends here.")

    def test_real_breaks_stay(self):
        for text in ("apples\nbananas\ncherries",
                     "Publications found\n244",
                     "His human cancer data\n8 + 18",
                     "Title\nBody text here.",
                     "First sentence here.\nSecond sentence here."):
            self.assertEqual(clean_text_for_speech(text), text)
        self.assertEqual(
            clean_text_for_speech("- one item that is long\n- two"),
            "one item that is long.\ntwo.")


if __name__ == "__main__":
    unittest.main()
