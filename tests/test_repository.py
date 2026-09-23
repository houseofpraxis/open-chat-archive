import json
import unittest
from pathlib import Path

from open_chat_archive.canonical import digest, revision_identity
from open_chat_archive.validation import validate_repository

ROOT = Path(__file__).resolve().parents[1]


class RepositoryTests(unittest.TestCase):
    def test_schemas_parse_and_declare_draft(self):
        for path in (ROOT / "schemas").rglob("*.json"):
            self.assertEqual(json.loads(path.read_text())["$schema"], "https://json-schema.org/draft/2020-12/schema")

    def test_repository_examples_and_contracts(self):
        self.assertEqual(len(validate_repository(ROOT)), 2)

    def test_revision_distinguishes_semantics_from_typed_storage_bookkeeping(self):
        record = {"spec_version":"0.1","record_type":"session","entity_id":"x","sensitivity":"restricted-unclassified","identity":{},"body": {"value": "x", "created_at": "source-time-one"}, "revision_id": "ignored"}
        first = revision_identity(record)
        record["body"]["created_at"] = "source-time-two"
        self.assertNotEqual(first, revision_identity(record))
        artifact = {**record, "record_type":"artifact", "body":{"byte_digest":"x", "storage_path":"blobs/a"}}
        stored = revision_identity(artifact); artifact["body"]["storage_path"]="blobs/b"
        self.assertEqual(stored, revision_identity(artifact))

    def test_skills_are_separate_and_named(self):
        for name in ("analyze-chat-export", "conform-markdown-transcript", "inspect-gathered-sessions"):
            text = (ROOT / "skills" / name / "SKILL.md").read_text()
            self.assertIn(f"name: {name}\n", text)

    def test_clean_room_record_names_reviewed_and_excluded_categories(self):
        text = (ROOT / "docs" / "clean-room.md").read_text()
        reviewed = "reviewed MyChatArchive's public README, advertised feature set, AGPL license, and repository file-level structure"
        excluded = "did not inspect or copy implementation bodies, tests, schemas, fixtures, or internal algorithms"
        self.assertIn(reviewed, text)
        self.assertIn(excluded, text)
        self.assertNotIn("no prose was inspected", text.lower())
