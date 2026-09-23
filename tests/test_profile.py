import contextlib, hashlib, io, json, os, tempfile, unittest
from pathlib import Path
from open_chat_archive.shape_profile import ProfileError, load_profile, main, profile_value

class ProfileTests(unittest.TestCase):
    def test_scalar_and_nested_key_canaries_are_absent(self):
        canaries=["alice@example.com","550e8400-e29b-41d4-a716-446655440000","https://secret.example/x","BEGIN_SECRET","private transcript"]
        value={canaries[0]:{canaries[1]:{canaries[2]:canaries[4]}},canaries[3]:"scalar-secret"}
        rendered=json.dumps(profile_value(value),sort_keys=True)
        for canary in canaries:
            self.assertNotIn(canary,rendered); self.assertNotIn(hashlib.sha256(canary.encode()).hexdigest(),rendered)
        self.assertEqual(profile_value({"a":1,"b":[]}),profile_value({"x":9,"y":[]}))

    def test_strict_inputs_limits_symlink_and_safe_diagnostics(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); bad=root/"opaque"
            cases=[b'\xef\xbb\xbf{}',b'{"x":1,"x":2}',b'{"x":1.0}',b'{"x":NaN}']
            for raw in cases:
                bad.write_bytes(raw)
                with self.assertRaises(ProfileError): load_profile(bad,"json",max_bytes=100,max_depth=2,max_items=5,max_keys=5)
            good=root/"good"; good.write_text('{}'); link=root/"link"; link.symlink_to(good)
            with self.assertRaises(ProfileError): load_profile(link,"json",max_bytes=100,max_depth=2,max_items=5,max_keys=5)
            stderr=io.StringIO(); bad.write_text('SECRET_CANARY')
            with contextlib.redirect_stderr(stderr): self.assertEqual(main([str(bad)]),2)
            self.assertNotIn("SECRET_CANARY",stderr.getvalue()); self.assertNotIn(str(bad),stderr.getvalue())
            with self.assertRaises(ProfileError): load_profile(good,"json",max_bytes=-1,max_depth=2,max_items=5,max_keys=5)

    def test_protected_key_map_is_restricted(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); source=root/"source"; target=root/"map"; source.write_text('{"raw-secret-key":1}')
            load_profile(source,"json",max_bytes=100,max_depth=3,max_items=10,max_keys=10,key_map_path=target)
            self.assertEqual(target.stat().st_mode & 0o777,0o600); self.assertIn("restricted-model-forbidden",target.read_text())

    def test_protected_map_retains_each_aggregated_key_coordinate(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); source=root/"source"; target=root/"map"
            source.write_text('[{"first-secret":1},{"second-secret":2}]')
            load_profile(source,"json",max_bytes=200,max_depth=4,max_items=20,max_keys=10,key_map_path=target)
            aliases=json.loads(target.read_text())["aliases"]
            self.assertEqual(len(aliases["/*/k0001"]),2)

if __name__ == "__main__": unittest.main()
