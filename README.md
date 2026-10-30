# German Audio Subtitles

German captions derived from the **spoken German dub**, with occasional English vocabulary help for an A2 learner progressing toward B1. A Firefox extension follows the Disney+ player while a local Python service transcribes the German audio already buffered in Firefox's disk cache.

The service processes upcoming audio where available, saves captions and glosses in SQLite, and reuses them when you pause, rewind or watch again. It follows movie switches and seeks automatically. Each movie/episode and audio rendition has its own timeline and history.

## Status and requirements

Version 0.2.0 is a local prototype, tested live with Firefox 157 on Linux, including Coco, Incredibles 2 and Inside Out 2. Observed preparation lead was roughly 19–24 seconds; it depends on buffering, network latency and recognition speed. **Compatibility with every Disney+ title is not guaranteed.** The selected German audio must be present as clear fragmented MP4 in Firefox's disk cache. Encrypted audio, private browsing and unavailable cache data are unsupported. This tool does not decrypt DRM, fetch the complete movie, or record system audio.

You need Firefox 142 or later, Linux, Python 3.11+, `ffmpeg`, [uv](https://docs.astral.sh/uv/), and an OpenAI API key with API credit. A Disney+ subscription alone does not provide OpenAI API credit. Node.js 22+ is needed only for extension tests and linting. The current transcription provider is `whisper-1`; vocabulary help uses `gpt-6.1-terra` through the Responses API.

## Install the service

```sh
git clone https://github.com/Idate96/german-audio-subtitles.git ~/git/german-audio-subtitles
cd ~/git/german-audio-subtitles
uv sync
```

Save your OpenAI key in a private file outside the repository, for example `~/.config/german-audio-subtitles/openai-key.txt`, and restrict its permissions to your user. Open a movie in Firefox briefly so its cache directory exists. Then configure:

```sh
uv run python server.py setup --key-file ~/.config/german-audio-subtitles/openai-key.txt
```

Setup can discover a single Linux Firefox cache automatically. If there are several profiles, supply the active profile's `cache2/entries` directory:

```sh
uv run python server.py setup --cache-directory /absolute/path/to/cache2/entries --key-file /absolute/path/to/api-key-file
```

Common cache roots are `~/.cache/mozilla/firefox/` and `~/snap/firefox/common/.cache/mozilla/firefox/`. Configuration lives outside the checkout. Start the service:

```sh
uv run python server.py serve
```

It listens only on `127.0.0.1:8766`, validates the Host header and requires the local pairing token. Read that token from `~/.config/german-audio-subtitles/pairing-code.txt`. It is separate from the API key.

## Load the Firefox extension

```sh
python3 build_extension.py
```

1. Open `about:debugging#/runtime/this-firefox`, choose **Load Temporary Add-on**, and select `dist/German-Audio-Subtitles.zip`.
2. Open the extension's toolbar popup. Paste the pairing code and click **Connect**.
3. Refresh the Disney+ player once so the extension observes the MediaSource from creation.
4. Select **German** audio without audio description, turn Disney+'s own subtitles off, and play. Newly buffered dialogue needs a short preparation period.

The toolbar switch disables captions and new work. The **Retry** button retries after API billing/access problems. Turning off Firefox's data-sharing permission for the extension also prevents operation.

### Permanent installation

Regular Firefox requires Mozilla's signature for permanent add-on installation. A temporary add-on disappears when Firefox restarts.

For a private install, [create or sign in to a Mozilla Add-ons developer account](https://addons.mozilla.org/developers/addon/submit/distribution), choose **On your own** (unlisted), and upload the generated ZIP. The source is readable JavaScript with no bundling or minification; no separate generated-source package is needed. Once Mozilla signs it, download the signed `.xpi` and install it through `about:addons` → gear → **Install Add-on From File…**. Reconnect with the same local pairing code if requested.

Unlisted means the add-on is not in the public catalog; Mozilla still receives its source for signing. Upload only the generated ZIP, never configuration or pairing files. This repository does not contain a Mozilla-signed release. See [Mozilla's signing guide](https://extensionworkshop.com/documentation/publish/signing-and-distribution-overview/) for current requirements.

### Optional login startup

Run `python3 install_service.py` from the checkout after setup. This installs and enables a **user** systemd service using the absolute checkout and uv paths. It does not need root. Inspect it with:

```sh
systemctl --user status german-audio-subtitles.service
journalctl --user -u german-audio-subtitles.service -n 20
```

Stop automatic startup with `systemctl --user disable --now german-audio-subtitles.service`.

## Storage and reuse

| Content | Default location |
| --- | --- |
| Code | Any checkout, such as `~/git/german-audio-subtitles` |
| Private config and pairing code | `~/.config/german-audio-subtitles/` |
| Captions, completed audio segments and glosses | `~/.local/share/german-audio-subtitles/subtitles.sqlite3` |
| Temporary decoded audio | `~/.cache/german-audio-subtitles/` |

Standard XDG environment overrides are supported. `--config` selects a different config file. Optional `state_directory` and `work_directory` config fields override data and temporary-audio locations. Keep those directories outside source control.

`additional_cache_directories` can register other Firefox profiles' `cache2/entries` directories. Entries from different profiles remain distinct even if their cache filenames match. The development launcher registers its own cache while preserving the primary profile's configuration.

Movie identity comes from the current tab URL, not the original SPA document URL. Per-tab media requests identify the selected rendition from cached HLS language metadata. The observed audio SourceBuffer timestamp offset maps the video clock to the audio timeline. Captions are withheld when identity or timing is uncertain. A request finishing after navigation saves its result to the original movie only.

Transcription owns each completed audio segment once and uses only two seconds of preceding context. Glosses, including empty results, are cached locally. Terra receives nearby dialogue and a stable cached instruction prefix; it returns exact German spans and brief English meanings. Code inserts parentheses without rewriting the German or its timestamps. German captions remain available if the glossary request is late or fails.

## Accuracy and limits

- Speech recognition can mishear names, songs, shouting and overlapping dialogue. Captions are not a verified transcript. A local voice check reduces invented text over music/silence but may miss quiet or sung voices.
- Seeking beyond prepared audio can cause a short gap. The extension never pauses or seeks the player automatically.
- A different movie cut or audio rendition has a distinct cache namespace. The tool prioritizes the currently reported playback; simultaneous movies in multiple tabs are not a supported use case.
- Firefox cache formats and Disney+ delivery/player behavior can change. This prototype depends on both.
- The optional local fallback uses a predownloaded faster-whisper `medium` model. It is disabled in a fresh setup. To enable it, download that model with faster-whisper first, then set `local_fallback` to `true` in the private config. No fallback model is silently downloaded.
- Billing/access failures pause paid transcription. Existing captions remain reusable; retry after resolving the API problem.

See [PRIVACY.md](PRIVACY.md) for exactly what stays local and what is sent to OpenAI.

## Development

### Local automatic reloading

```sh
npm ci --ignore-scripts
npm run dev
```

This opens a dedicated Firefox development profile, loads the extension temporarily, and reloads it automatically whenever a file in `extension/` changes. The profile persists between launches, including its Disney+ login. Sign in once in this window. The launcher pairs the extension with your local service and reuses the existing subtitle database.

To add a desktop/application shortcut:

```sh
python3 dev_firefox.py --install-launcher
```

Then open **Firefox — German subtitles** from the application menu or desktop. Launch through that shortcut each time to load the extension and start its file watcher. This is a development session: ordinary Firefox still requires signed extension updates. Mozilla's [web-ext guide](https://extensionworkshop.com/documentation/develop/getting-started-with-web-ext/) describes this supported reload mechanism.

The launcher uses a separate profile because `web-ext` enables debugging preferences there. It never applies those preferences to your daily profile. On Snap Firefox, the dedicated profile, audio cache and private development extension copy live under `~/snap/firefox/common/german-audio-subtitles-development/`; other installations use the app's XDG data directory. The generated development copy contains only a local pairing token, remains outside the repository, and is never used by `build_extension.py`. Launcher output is logged in `~/.local/state/german-audio-subtitles/firefox-development.log`.

Local backend changes, such as Terra prompts or transcription logic, need a restart of `german-audio-subtitles.service`; they do not require another Mozilla-signed extension. Normal Firefox extension updates need a new signature; readable JavaScript in this project does not need a separate pre-minification source archive.

### Checks

```sh
uv run python -m unittest discover -v
npm ci --ignore-scripts
npm test
npm run lint
python3 build_extension.py
```

Tests exercise clock mapping, seeks, SPA movie switches, stale asynchronous replies, per-tab evidence, pairing authorization, durable storage and duplicate-request avoidance. Unit tests and lint do not replace visible playback verification. The build uses a manifest-derived allowlist and never reads service configuration or includes recordings, databases, pairing codes or keys.

Primary references: [Firefox cache format](https://firefox-source-docs.mozilla.org/networking/cache2/doc.html), [Mozilla temporary installation](https://extensionworkshop.com/documentation/develop/temporary-installation-in-firefox/), [OpenAI speech-to-text](https://developers.openai.com/api/docs/guides/speech-to-text), [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching).
