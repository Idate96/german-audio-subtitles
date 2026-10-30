import json
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile

from build_extension import build
from cache_audio import FirefoxCache
from dev_firefox import configure_cache, mirror_extension


class DevelopmentTests(unittest.TestCase):
    def test_pairing_is_only_in_private_copy_and_source_edits_are_mirrored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, private = root / "source", root / "private"
            source.mkdir()
            manifest = {"version":"test", "background":{"scripts":["background.js"]}, "content_scripts":[]}
            (source / "manifest.json").write_text(json.dumps(manifest))
            for name in ["background.js", "popup.html", "popup.js", "icon.svg"]:
                (source / name).write_text('const x = browser.storage.local.get({enabled: true, serviceToken: ""}); // original')
            token = "development-test-pairing-" + "a" * 32
            first, changed = mirror_extension(source, private, token)
            self.assertTrue(changed)
            self.assertIn(token, (private / "background.js").read_text())
            self.assertNotIn(token, (source / "background.js").read_text())
            self.assertFalse(mirror_extension(source, private, token)[1])
            (source / "background.js").write_text('const x = browser.storage.local.get({enabled: true, serviceToken: ""}); // edited')
            second, changed = mirror_extension(source, private, token)
            self.assertTrue(changed); self.assertNotEqual(first, second)
            self.assertIn("// edited", (private / "background.js").read_text())
            build(source, root / "release.zip")
            with zipfile.ZipFile(root / "release.zip") as archive:
                self.assertNotIn(token.encode(), b"".join(archive.read(n) for n in archive.namelist()))

    def test_cache_registration_preserves_main_profile_token_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); config=root / "config.json"
            original={"token":"test", "cache_directory":"/original/cache"}
            config.write_text(json.dumps(original))
            updated, changed=configure_cache(config, root / "dev-cache")
            self.assertTrue(changed)
            self.assertEqual(updated["token"], original["token"])
            self.assertEqual(updated["cache_directory"], original["cache_directory"])
            self.assertFalse(configure_cache(config, root / "dev-cache")[1])

    def test_two_profile_caches_with_same_filename_are_independent(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for profile in ["normal", "development"]:
                directory=root / profile; directory.mkdir()
                data=profile.encode()
                metadata=b'\x00https://test.media.dssott.com/' + profile.encode() + b'.mp4a\x00content-type: audio/mp4\r\n\x00'
                (directory / "same-hash").write_bytes(data + metadata + struct.pack(">I", len(data)))
            cache=FirefoxCache(root / "normal", [root / "development", root / "missing"])
            cache.refresh()
            self.assertEqual(len(cache.entries), 2)
            self.assertEqual({entry.read() for entry in cache.entries.values()}, {b"normal", b"development"})
            (root / "development/same-hash").unlink()
            cache.refresh()
            self.assertEqual(len(cache.entries),1)
            self.assertEqual(next(iter(cache.entries.values())).read(),b"normal")


if __name__ == "__main__":
    unittest.main()
