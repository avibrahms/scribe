"""Offline regressions for where the Edge voice is kept: config.json, with
settings.json as the helpers' mirror, and nothing else."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scribe_core as core


class VoiceStoreTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        for name, path in (("CONFIG_FILE", d / "config.json"),
                           ("SETTINGS_FILE", d / "settings.json"),
                           ("VOICE_CONFIG_FILE", d / "config")):
            self.addCleanup(setattr, core, name, getattr(core, name))
            setattr(core, name, path)

    def test_default_when_nothing_is_saved(self):
        self.assertEqual(core.load_voice(), core.DEFAULT_VOICE)
        self.assertEqual(core.load_tts_engine(), "edge")

    def test_voice_kept_in_the_old_file_moves_to_config_json(self):
        core.VOICE_CONFIG_FILE.write_text(
            'VOICE="fr-FR-HenriNeural"\nRATE="+10%"\n')
        self.assertEqual(core.load_voice(), "fr-FR-HenriNeural")
        core.sync_speak_settings()
        self.assertFalse(core.VOICE_CONFIG_FILE.exists())
        self.assertEqual(core.load_voice(), "fr-FR-HenriNeural")
        self.assertEqual(
            json.loads(core.SETTINGS_FILE.read_text())["voice"],
            "fr-FR-HenriNeural")

    def test_saved_voice_reaches_the_helpers(self):
        core.save_voice("en-AU-WilliamNeural")
        core.save_edge_prosody(rate="+10%")
        mirror = json.loads(core.SETTINGS_FILE.read_text())
        self.assertEqual(mirror["voice"], "en-AU-WilliamNeural")
        self.assertEqual(mirror["rate"], "+10%")
        self.assertEqual(core.load_voice(), "en-AU-WilliamNeural")
        self.assertFalse(core.VOICE_CONFIG_FILE.exists())


if __name__ == "__main__":
    unittest.main()
