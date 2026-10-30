import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from build_extension import build


class PackageTests(unittest.TestCase):
    def test_build_ignores_private_files_and_does_not_need_service_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = {"version": "test", "background": {"scripts": ["background.js"]}, "content_scripts": []}
            (root / "manifest.json").write_text(json.dumps(manifest))
            for file in ["background.js", "popup.html", "popup.js", "icon.svg"]:
                (root / file).write_text("test")
            (root / "local-config.js").write_text("private-pairing-token")
            output = root / "build.zip"
            build(root, output)
            with zipfile.ZipFile(output) as archive:
                self.assertNotIn("local-config.js", archive.namelist())
                self.assertNotIn(b"private-pairing-token", b"".join(archive.read(p) for p in archive.namelist()))
            manifest["background"]["scripts"].append("local-config.js")
            (root / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                build(root, output)


if __name__ == "__main__":
    unittest.main()
