"""Launch a dedicated Firefox profile with automatic extension reloads."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import signal
import subprocess
import threading
import time

ROOT = Path(__file__).resolve().parent


def mirror_extension(source, destination, token):
    """Create a private development copy; the public package never includes it."""
    manifest = json.loads((source / "manifest.json").read_text())
    names = {"manifest.json", "popup.html", "popup.js", "icon.svg"}
    names.update(manifest["background"]["scripts"])
    for script in manifest["content_scripts"]:
        names.update(script["js"])
    contents = {}
    for name in sorted(names):
        if Path(name).name != name or name == "local-config.js":
            raise ValueError("Unexpected development source path")
        contents[name] = (source / name).read_bytes()
    # Pair this local-only profile before its background starts. This file is
    # generated outside the checkout and never used by the release builder.
    anchor = b'browser.storage.local.get({enabled: true, serviceToken: ""})'
    if contents["background.js"].count(anchor) != 1:
        raise ValueError("The background settings loader changed; update the development pairing hook")
    paired = ("browser.storage.local.set({serviceToken:" + json.dumps(token)
              + "}).then(() => " + anchor.decode() + ")").encode()
    contents["background.js"] = contents["background.js"].replace(anchor, paired)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256()
    changed = False
    for name, data in contents.items():
        digest.update(name.encode() + b"\0" + data)
        target = destination / name
        if target.exists() and target.read_bytes() == data:
            continue
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(data)
        temporary.chmod(0o600)
        temporary.replace(target)
        changed = True
    return digest.hexdigest(), changed


def configure_cache(config_path, cache_directory):
    config = json.loads(config_path.read_text())
    extra = config.get("additional_cache_directories", [])
    path = str(cache_directory / "cache2/entries")
    changed = path not in extra
    if changed:
        config["additional_cache_directories"] = [*extra, path]
        temporary = config_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2))
        temporary.chmod(0o600)
        temporary.replace(config_path)
    return config, changed


def install_launcher():
    executable = Path.home() / ".local/bin/german-subtitles-firefox"
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "german-audio-subtitles"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\numask 077\nexec " + shlex.join([
        shutil.which("python3"), str(ROOT / "dev_firefox.py")])
        + " >> " + shlex.quote(str(state / "firefox-development.log")) + " 2>&1\n")
    executable.chmod(0o755)
    applications = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    entry = applications / "german-subtitles-firefox.desktop"
    escaped = str(executable).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    entry.write_text("[Desktop Entry]\nType=Application\nName=Firefox — German subtitles\n"
        "Comment=Disney+ with automatically loaded subtitles and local extension reloads\n"
        + 'Exec="' + escaped + '"\nIcon=firefox\nTerminal=false\nCategories=AudioVideo;Education;\nStartupNotify=true\n')
    entry.chmod(0o755)
    desktop = Path.home() / "Desktop"
    if desktop.is_dir():
        shortcut = desktop / entry.name
        shutil.copy2(entry, shortcut)
        subprocess.run(["gio", "set", str(shortcut), "metadata::trusted", "true"], check=False)
    print("Installed launcher: Firefox — German subtitles\nCommand: " + str(executable))


def main():
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=config_home / "german-audio-subtitles/config.json")
    firefox = "/snap/bin/firefox" if Path("/snap/bin/firefox").exists() else shutil.which("firefox")
    parser.add_argument("--firefox", default=firefox)
    parser.add_argument("--url", default="https://www.disneyplus.com/")
    parser.add_argument("--headless", action="store_true", help="For development verification")
    parser.add_argument("--install-launcher", action="store_true", help="Install a desktop/application shortcut")
    args = parser.parse_args()
    if args.install_launcher:
        install_launcher()
        return
    runner = ROOT / "node_modules/.bin/web-ext"
    if not runner.exists():
        raise SystemExit("Run npm ci --ignore-scripts in the checkout first.")
    if not args.firefox:
        raise SystemExit("Firefox was not found; supply --firefox /path/to/firefox.")
    # Snap Firefox can access its common directory across revisions. Other
    # installations use the normal private XDG data directory.
    if "/snap/" in args.firefox:
        runtime = Path.home() / "snap/firefox/common/german-audio-subtitles-development"
    else:
        runtime = data_home / "german-audio-subtitles/firefox-development"
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (runtime / "launcher.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("The German subtitles development Firefox is already running.")
    profile, cache, source = runtime / "profile", runtime / "cache", runtime / "extension"
    config, changed = configure_cache(args.config.expanduser(), cache)
    if changed:
        active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", "german-audio-subtitles.service"])
        if active.returncode == 0:
            subprocess.run(["systemctl", "--user", "restart", "german-audio-subtitles.service"], check=True)
        else:
            print("Restart the local subtitle service to read this profile's cache.", flush=True)
    mirror_extension(ROOT / "extension", source, config["token"])
    command = [str(runner), "run", "--no-config-discovery", "--no-input",
        "--source-dir", str(source), "--firefox", args.firefox,
        "--firefox-profile", str(profile), "--profile-create-if-missing", "--keep-profile-changes",
        "--start-url", args.url, "--pref", "browser.cache.disk.parent_directory=" + str(cache),
        "--pref", "browser.shell.checkDefaultBrowser=false"]
    if args.headless:
        command += ["--args=-headless"]
    stop = threading.Event()

    def mirror():
        while not stop.wait(.5):
            try:
                _, changed = mirror_extension(ROOT / "extension", source, config["token"])
                if changed:
                    print("Extension source changed; Firefox will reload it.", flush=True)
            except (OSError, ValueError, KeyError):
                # Editors can replace several files while a save is in progress.
                continue

    process = subprocess.Popen(command, cwd=ROOT, start_new_session=True)
    watcher = threading.Thread(target=mirror, daemon=True)
    watcher.start()

    def interrupted(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        raise SystemExit(process.wait())
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


if __name__ == "__main__":
    main()
