import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SpecificationLayoutTests(unittest.TestCase):
    def test_normative_document_layout(self):
        required = (
            "docs/clean-room.md",
            "docs/spec/v0.1/README.md",
            "docs/spec/v0.1/data-model.md",
            "docs/spec/v0.1/serialization.md",
            "docs/spec/v0.1/adapter-contract.md",
            "docs/spec/v0.1/security.md",
            "docs/spec/v0.1/decisions.md",
        )
        for relative_path in required:
            with self.subTest(path=relative_path):
                self.assertTrue((ROOT / relative_path).is_file())

    def test_spec_index_links_normative_documents(self):
        index = (ROOT / "docs/spec/v0.1/README.md").read_text(encoding="utf-8")
        for name in ("data-model.md", "serialization.md", "adapter-contract.md", "security.md", "decisions.md"):
            with self.subTest(name=name):
                self.assertIn(f"]({name})", index)


if __name__ == "__main__":
    unittest.main()
