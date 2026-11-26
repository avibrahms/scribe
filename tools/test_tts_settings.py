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

    def test_speechify_settings_reach_the_helpers(self):
        core.save_speechify_voice("wyatt_32")
        core.save_speechify_speed(1.25)
        core.save_speechify_voice_for_lang("fr", "romain-agent")
        mirror = json.loads(core.SETTINGS_FILE.read_text())
        self.assertEqual(mirror["speechify_voice"], "wyatt_32")
        self.assertEqual(mirror["speechify_speed"], 1.25)
        self.assertEqual(mirror["speechify_voice_by_lang"], {"fr": "romain-agent"})
        self.assertEqual(mirror["speechify_voice_langs"]["romain-agent"], "fr")
        self.assertEqual(core.effective_tts_voice("speechify", "fr"), "romain-agent")
        # A language never picked speaks with a voice native to it.
        self.assertEqual(core.effective_tts_voice("speechify", "de"), "henrik-agent")
        self.assertEqual(core.effective_tts_voice("speechify", None), "wyatt_32")

    def test_each_language_can_have_its_own_engine(self):
        core.save_tts_engine("edge")
        core.save_tts_engine_for_lang("en", "speechify")
        core.save_tts_engine_for_lang("fr", "openai")  # unknown: ignored
        mirror = json.loads(core.SETTINGS_FILE.read_text())
        self.assertEqual(mirror["engine"], "edge")
        self.assertEqual(mirror["engine_by_lang"], {"en": "speechify"})
        self.assertEqual(core.effective_tts_engine("en"), "speechify")
        self.assertEqual(core.effective_tts_engine("fr"), "edge")
        self.assertEqual(core.effective_tts_engine(None), "edge")
        english = "This is the text that we would like to hear, and it is in English."
        french = "Voici le texte que nous voulons entendre, et il est en français."
        self.assertEqual(core.resolve_tts_engine(english), ("speechify", "en"))
        self.assertEqual(core.resolve_tts_engine(french), ("edge", "fr"))
        # The hotkey launcher picks the same engine from the mirror.
        sys.path.insert(0, str(Path(core.__file__).parent / "bin"))
        import tts_engine
        self.assertEqual(tts_engine.pick_engine(english, mirror), "speechify")
        self.assertEqual(tts_engine.pick_engine(french, mirror), "edge")
        core.save_tts_autodetect(False)
        self.assertEqual(core.resolve_tts_engine(english), ("edge", None))
        mirror = json.loads(core.SETTINGS_FILE.read_text())
        self.assertEqual(tts_engine.pick_engine(english, mirror), "edge")

    def test_openai_settings_are_dropped_and_the_speed_kept(self):
        core.CONFIG_FILE.write_text(json.dumps(
            {"edge_voice": "fr-FR-HenriNeural", "tts_engine": "openai",
             "openai_voice": "fable", "openai_speed": 1.25}))
        core.SETTINGS_FILE.write_text(json.dumps(
            {"voice": "fr-FR-HenriNeural", "openai_voice": "fable",
             "openai_voice_by_lang": {"en": "ash"}}))
        core.sync_speak_settings()
        cfg = json.loads(core.CONFIG_FILE.read_text())
        mirror = json.loads(core.SETTINGS_FILE.read_text())
        self.assertFalse([k for k in list(cfg) + list(mirror) if "openai" in k])
        self.assertEqual(mirror["speechify_speed"], 1.25)
        self.assertEqual(mirror["engine"], "edge")
        self.assertEqual(mirror["voice"], "fr-FR-HenriNeural")


if __name__ == "__main__":
    unittest.main()
