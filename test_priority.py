import unittest
from types import SimpleNamespace
from priority import source_priority


class PriorityTests(unittest.TestCase):
    def test_original_header_survives_parser_text_replacement(self):
        event = SimpleNamespace(event_id=1, text="Message body")
        result = source_priority(event, {"1": {"text": "Dôležitá správa: Example"}})
        self.assertTrue(result["is_important"])
        self.assertEqual(result["source_text"], "Dôležitá správa: Example")

    def test_bookmark_is_not_sender_importance(self):
        event = SimpleNamespace(event_id=1, is_starred=True)
        result = source_priority(event, {"1": {"text": "Ordinary message"}})
        self.assertFalse(result["is_important"])
        self.assertTrue(result["is_starred"])

    def test_missing_source_is_unknown(self):
        self.assertIsNone(source_priority(SimpleNamespace(event_id=2), {})["is_important"])
