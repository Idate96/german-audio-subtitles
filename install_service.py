"""Install the configured local service for the current Linux user."""
import argparse
import os
from pathlib import Path
import subprocess


def systemd_quote(path):
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def main():
    root = Path(__file__).resolve().parent
    config_root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=config_root / "german-audio-subtitles/config.json")
    parser.add_argument("--no-start", action="store_true", help="Write/reload the unit without enabling or starting it")
    args = parser.parse_args()
    python = root / ".venv/bin/python"
    if not python.exists():
        raise SystemExit("Run uv sync in the checkout first.")
    if not args.config.expanduser().is_file():
        raise SystemExit("Run server.py setup first, or supply --config.")
    if not args.no_start and subprocess.run(["systemctl", "--user", "is-active", "--quiet",
                                           "coco-german-subtitles.service"]).returncode == 0:
        raise SystemExit("The earlier coco-german-subtitles.service is running. Migrate/stop it before starting a second service.")
    unit = config_root / "systemd/user/german-audio-subtitles.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("\n".join([
        "[Unit]", "Description=German audio subtitles for Firefox", "After=network-online.target", "",
        "[Service]", "Type=simple", "WorkingDirectory=" + str(root).replace("%", "%%"),
        "ExecStart=" + " ".join([systemd_quote(python), systemd_quote(root / "server.py"),
                                  "serve", "--config", systemd_quote(args.config.expanduser().resolve())]),
        "Restart=on-failure", "RestartSec=5", "UMask=0077", "", "[Install]", "WantedBy=default.target", ""
    ]))
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    if not args.no_start:
        subprocess.run(["systemctl", "--user", "enable", "--now", "german-audio-subtitles.service"], check=True)
    print("Installed " + str(unit))


if __name__ == "__main__":
    main()
