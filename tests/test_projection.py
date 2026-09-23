"""Canonical projection helpers."""
from __future__ import annotations

import unittest

from open_chat_archive.projection import canonical_datetime, canonical_model, json_pointer, profile_path_for_pointer


class ProjectionTests(unittest.TestCase):
    def test_datetime_and_model(self) -> None:
        self.assertEqual(canonical_datetime("2026-01-01T00:00:00Z"), "2026-01-01T00:00:00Z")
        self.assertEqual(canonical_datetime(1_704_067_200), "2024-01-01T00:00:00Z")
        self.assertIsNone(canonical_datetime("not-a-date"))
        self.assertEqual(canonical_model("m", provider="open-webui"), {"provider": "open-webui", "model_name": "m"})
        self.assertIsNone(canonical_model("", provider="open-webui"))

    def test_pointer_alias(self) -> None:
        self.assertEqual(json_pointer(0, "chat_messages", 1), "/0/chat_messages/1")
        root = [{"name": "Synthetic", "uuid": "a"}]
        self.assertEqual(profile_path_for_pointer(root, "/0/name"), "/*/k0001")


if __name__ == "__main__":
    unittest.main()
