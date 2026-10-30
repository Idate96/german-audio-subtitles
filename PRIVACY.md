# Data handling

The Firefox extension observes playback timing and completed audio/playlist requests on Disney+ tabs. It sends the current title, playback URL, timing diagnostics and canonical media paths to a paired service on `127.0.0.1:8766`. Media URL queries and long signed path prefixes are removed. It does not send Disney+ cookies, account passwords, authorization headers or response bodies to that service.

The service reads matching German audio already present in the selected Firefox HTTP cache. It accepts clear audio only. Small decoded clips are sent to OpenAI's transcription API using the user's API key. Nearby German caption text is sent to the Responses API for optional English vocabulary glosses. Responses requests set `store: false`; OpenAI's applicable API data policies still apply. A local voice detector skips clips without detected speech. An optional local Whisper fallback can transcribe without uploading audio, but the glossary feature still uses OpenAI.

Original captions, completed segment identifiers and glossary results are stored in a local SQLite database for reuse. Temporary decoded audio is deleted after each job. Saved learning data is not sent to the extension repository or its author. There is no analytics service.

The OpenAI key is read from a local file and never placed in the extension. A separate local pairing token is stored in the service configuration and Firefox extension storage. Keep configuration and pairing files private. The extension build contains neither token.

The default locations are:

- Configuration and pairing code: `~/.config/german-audio-subtitles/`
- Saved captions and glosses: `~/.local/share/german-audio-subtitles/subtitles.sqlite3`
- Temporary decoded audio: `~/.cache/german-audio-subtitles/`

The service honors `XDG_CONFIG_HOME`, `XDG_DATA_HOME` and `XDG_CACHE_HOME`. Disabling the extension or closing playback stops new jobs after at most 15 seconds; an already running API request can finish. Delete the local data directory while the service is stopped to erase saved captions.
