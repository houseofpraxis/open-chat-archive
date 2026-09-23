import json,tempfile,unittest
from pathlib import Path
from open_chat_archive.adapter_verifier import verify_scaffold
from open_chat_archive.scaffold_adapter import scaffold
from open_chat_archive.shape_profile import profile_value

class AdapterTests(unittest.TestCase):
    def test_scaffold_and_local_verifier(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); package=root/"adapter"; profile=profile_value({"synthetic":True}); scaffold(profile,package)
            verify_scaffold(package,root/"work",["REAL_SECRET_CANARY"])
            self.assertTrue((package/"OWNED_FILES.txt").is_file())
    def test_unknown_and_ambiguous_are_rejected(self):
        from open_chat_archive.adapter import FingerprintResult,select_adapter
        class A:
            def recognize(self,p): return FingerprintResult(p.get("yes",False))
        with self.assertRaisesRegex(ValueError,"unknown"): select_adapter([A()],{})
        with self.assertRaisesRegex(ValueError,"ambiguous"): select_adapter([A(),A()],{"yes":True})
    def test_scaffold_rejects_unversioned_or_raw_paths(self):
        with tempfile.TemporaryDirectory() as d:
            profile=profile_value({"safe":True}); profile["paths"][1]["path"]="/raw-secret-key"
            with self.assertRaisesRegex(ValueError,"profile-path"): scaffold(profile,Path(d)/"adapter")
if __name__ == "__main__": unittest.main()
