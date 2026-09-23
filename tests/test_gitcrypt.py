import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class GitCryptGateTests(unittest.TestCase):
    def test_ciphertext_header_is_required_for_every_media_type(self):
        spec = importlib.util.spec_from_file_location("cipher_gate", ROOT / "scripts" / "check_staged_ciphertext.py")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        self.assertTrue(module.is_git_crypt_ciphertext(b"\x00GITCRYPT\x00synthetic-ciphertext"))
        for plaintext in (b'{"json":true}', b"plain markdown\n", b"\x89PNG\r\n"):
            self.assertFalse(module.is_git_crypt_ciphertext(plaintext))


if __name__ == "__main__": unittest.main()
