"""Package only extension source; configuration and user data are never inputs."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile


def build(source, output):
    source, output = Path(source), Path(output)
    manifest = json.loads((source / "manifest.json").read_text())
    files = {"manifest.json", "popup.html", "popup.js", "icon.svg"}
    files.update(manifest["background"]["scripts"])
    for script in manifest["content_scripts"]:
        files.update(script["js"])
    contents = {}
    for name in sorted(files):
        if name != Path(name).name or name in {"local-config.js", "config.json"}:
            raise ValueError("Private configuration or an unexpected path is referenced by the manifest")
        data = (source / name).read_bytes()
        if re.search(rb"sk-(?:proj-)?[A-Za-z0-9_-]{24,}", data):
            raise ValueError("An API credential appears in extension source")
        contents[name] = data
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
    temporary.replace(output)
    return {"version": manifest["version"], "package": str(output), "files": sorted(contents),
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=root / "dist/German-Audio-Subtitles.zip")
    args = parser.parse_args()
    print(json.dumps(build(root / "extension", args.output), indent=2))


if __name__ == "__main__":
    main()
