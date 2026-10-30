#!/usr/bin/env python3
"""Loopback subtitle service. It never downloads media or decrypts DRM."""
import argparse
import difflib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit
import wave

import requests

from cache_audio import (FirefoxCache, cached_segments, contiguous_end, canonical_path,
                         select_audio_playlist, AudioSelectionError)
from subtitle_db import SubtitleDB
from glossary import PROFILE as GLOSS_PROFILE, annotate, GlossError
from local_transcribe import speech_intervals

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "german-audio-subtitles"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "german-audio-subtitles"
WORK_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "german-audio-subtitles"
CONFIG = CONFIG_DIR / "config.json"
COCO_ID = "ce1ccdca-f468-4960-b67c-026b01ba42ab"
LOG = logging.getLogger("german-subtitles")
MAX_TIME = 24 * 3600
BILLING_CODES = {"credit_balance_exhausted", "insufficient_quota", "organization_spend_limit_exceeded",
                 "project_spend_limit_exceeded", "organization_usage_limit_exceeded"}


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temporary.chmod(0o600)
    temporary.replace(path)


def read_key(path):
    match = re.search(r"sk-[A-Za-z0-9_-]+", Path(path).read_text())
    if not match:
        raise ValueError("No OpenAI API key found in the configured key file.")
    return match.group()


def movie_clock(raw_position, diagnostics):
    """MSE timestamps are presentation timestamps plus SourceBuffer.timestampOffset."""
    offset = diagnostics.get("mseTimestampOffset") if isinstance(diagnostics, dict) else None
    # Independent audio/video timebases can differ below a millisecond after a
    # seek. Accept observed, current-generation agreement within 2 ms; do not
    # confuse this numeric tolerance with choosing between different timelines.
    if offset is None and isinstance(diagnostics, dict):
        evidence = [b for b in diagnostics.get("mseBuffers", []) if isinstance(b, dict)
                    and b.get("association") in {"active-source", "fresh-context"}]
        audio = [b for b in evidence if b.get("kind") == "audio"]
        chosen = audio or evidence
        values = [b.get("offset") for b in chosen]
        if (chosen and all(b.get("eligible") is True for b in chosen)
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values)
                and max(values) - min(values) <= .002):
            offset = values[0]
    if isinstance(offset, bool) or not isinstance(offset, (int, float)) or not math.isfinite(offset):
        return None
    position = raw_position - offset
    if not 0 <= position <= MAX_TIME:
        return None
    return position, offset


class TranscriptionError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def filter_nonspeech(transcript, speech):
    """Reject whole recognition segments unsupported by any detected voice.

    Never retime or delete individual words inside a supported spoken segment.
    This targets Whisper's invented subtitle credits over music/silence.
    """
    if not speech:
        return {"text": "", "words": [], "segments": [], "silence": True}
    original = transcript.get("segments", [])
    if not original:
        return transcript
    kept = [s for s in original if any(s["end"] > start and s["start"] < end for start, end in speech)]
    if len(kept) == len(original):
        return transcript
    words = [w for w in transcript.get("words", [])
             if any(s["start"] <= (w["start"] + w["end"]) / 2 <= s["end"] for s in kept)]
    return {**transcript, "segments": kept, "words": words, "text": " ".join(s["text"].strip() for s in kept)}


def transcribe(wav, config):
    speech = speech_intervals(wav)
    if not speech:
        return filter_nonspeech({}, speech)
    with Path(wav).open("rb") as stream:
        response = requests.post("https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": "Bearer " + read_key(config["key_file"])},
            files={"file": ("audio.wav", stream, "audio/wav")},
            data=[("model", "whisper-1"), ("language", "de"), ("response_format", "verbose_json"),
                  ("timestamp_granularities[]", "word"), ("timestamp_granularities[]", "segment"),
                  ("temperature", "0"),
                  ("prompt", "Deutsche Filmsprache. Gesprochene Wörter wortgetreu, mit Satzzeichen; "
                             "auch kurze Wörter, Umgangssprache und fremdsprachige Namen beibehalten.")],
            timeout=(10, 90))
    if not response.ok:
        try:
            code = response.json().get("error", {}).get("code") or "http_" + str(response.status_code)
        except ValueError:
            code = "http_" + str(response.status_code)
        # Do not log response bodies: authentication errors can contain key fragments.
        raise TranscriptionError(code)
    return filter_nonspeech(response.json(), speech)


def caption_cues(transcript, offset, own_start, own_end):
    """Group word times into readable captions, owning each boundary word once."""
    words = []
    timed_words = [dict(w) for w in transcript.get("words", [])]
    # Whisper's separate word array can omit punctuation present in its transcript.
    tokens = (transcript.get("text") or " ".join(s.get("text", "") for s in transcript.get("segments", []))).split()
    normalize = lambda s: re.sub(r"[^\w]", "", s).casefold()
    matches = difflib.SequenceMatcher(a=[normalize(w.get("word", "")) for w in timed_words],
                                     b=[normalize(t) for t in tokens], autojunk=False)
    for block in matches.get_matching_blocks():
        for index in range(block.size):
            timed_words[block.a + index]["word"] = tokens[block.b + index]
    for word in timed_words:
        start, end = offset + float(word["start"]), offset + float(word["end"])
        text = str(word.get("word", "")).strip()
        # Whisper sometimes assigns a spoken short word a zero-width timestamp.
        # Keep it in its phrase instead of silently deleting part of the dialogue.
        if text and end >= start and own_start <= (start + end) / 2 < own_end:
            words.append({"start": max(start, own_start), "end": min(end, own_end), "text": text})
    if not words:
        return [{"start": max(own_start, offset + float(s["start"])),
                 "end": min(own_end, offset + float(s["end"])), "text": s["text"].strip()}
                for s in transcript.get("segments", [])
                if s.get("text", "").strip() and s.get("no_speech_prob", 0) < .7
                and offset + float(s["start"]) < own_end and offset + float(s["end"]) > own_start]
    result, group = [], []

    def flush():
        if group:
            result.append({"start": group[0]["start"], "end": group[-1]["end"],
                           "text": " ".join(w["text"] for w in group)})
            if result[-1]["end"] <= result[-1]["start"]:
                result[-1]["end"] = min(own_end, result[-1]["start"] + .08)
            group.clear()

    for word in words:
        if group and (word["start"] - group[-1]["end"] > .7
                      or word["end"] - group[0]["start"] > 5.5
                      or len(" ".join(w["text"] for w in group)) + len(word["text"]) > 78):
            flush()
        group.append(word)
        if re.search(r"[.!?…][\"»”]*$", word["text"]) and word["end"] - group[0]["start"] >= 1:
            flush()
    flush()
    return result


def choose_batch(segments, completed, position):
    candidates = [s for s in segments if s.key not in completed and s.end > position - 1
                  and s.start < position + 90]
    if not candidates:
        return [], []
    first = candidates[0]
    owned = [first]
    for segment in candidates[1:]:
        if abs(segment.start - owned[-1].end) > .12 or segment.end - first.start > 32.1:
            break
        owned.append(segment)
    # One earlier fragment gives context to words that cross a chunk boundary.
    previous = next((s for s in reversed(segments) if abs(s.end - first.start) <= .12
                     and s.init.path == first.init.path), None)
    clip = ([previous] if previous else []) + owned
    # A map switch marks a new initialization context; don't mix it into a clip.
    clip = [s for s in clip if s.init.path == first.init.path]
    owned = [s for s in owned if s.init.path == first.init.path]
    return clip, owned


def decode(segments, directory, own_start=None):
    media = directory / "clip.m4a"
    audio = directory / "clip.wav"
    media.write_bytes(segments[0].init.read() + b"".join(s.entry.read() for s in segments))
    # Keep just two seconds of overlap rather than retranscribing a whole earlier fragment.
    trim = max(0, own_start - segments[0].start - 2) if own_start is not None else 0
    result = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(media), "-ss", str(trim), "-map", "0:a:0",
                             "-ac", "1", "-ar", "16000", str(audio)], capture_output=True, timeout=20)
    if result.returncode or result.stderr:
        raise ValueError("Cached audio could not be decoded cleanly.")
    with wave.open(str(audio)) as stream:
        duration = stream.getnframes() / stream.getframerate()
    expected = sum(s.duration for s in segments) - trim
    if abs(duration - expected) > .15:
        raise ValueError("Decoded audio duration does not match the cached timeline.")
    return audio, duration, segments[0].start + trim


class Engine:
    def __init__(self, config):
        self.config = config
        self.cache = FirefoxCache(config["cache_directory"])
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.gloss_wake = threading.Event()
        self.last_sync = 0
        self.position = 0
        self.raw_position = 0
        self.clock_offset = 0
        self.clock_ready = False
        self.track_ready = False
        self.playback_id = None
        self.title = ""
        self.playlist_path = None
        self.generation = 0
        self.media_requests = []
        self.diagnostics = {}
        self.segments = []
        self.state = "waiting"
        self.message = "Open a Disney+ title with German audio."
        self.retry_at = 0
        self.state_directory = Path(config.get("state_directory", DATA_DIR)).expanduser()
        self.work_directory = Path(config.get("work_directory", WORK_DIR)).expanduser()
        self.state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.work_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store_file = self.state_directory / "transcripts.json"
        legacy_path = config.get("audio_playlist_path", "")
        self.legacy_path = canonical_path(legacy_path)
        self.legacy_key = COCO_ID + ":de:" + hashlib.sha256(legacy_path.encode()).hexdigest()[:16]
        self.db = SubtitleDB(self.state_directory / "subtitles.sqlite3")
        if legacy_path:
            self.db.import_json(self.legacy_key, self.store_file)
        self.movie_key = None
        self.store = {"clips": []}
        self.completed = set()
        self.gloss_cache = {}
        self.gloss_failed = set()
        self.gloss_retry_at = 0
        self.gloss_blocked = False
        self.gloss_stats = {"state": "waiting", "requests": 0, "inputTokens": 0, "cachedInputTokens": 0,
                            "outputTokens": 0, "lastLatencyMs": None, "model": "gpt-6.1-terra"}
        self.blocked = config.get("billing_paused", False)
        if self.blocked:
            self.state = "error"
            self.message = "OpenAI API credits are exhausted. Add credits, then retry."

    def sync(self, data):
        page = urlsplit(str(data.get("pageUrl", "")))
        match = re.search(r"/play/([a-zA-Z0-9-]{8,128})(?:/|$)", page.path)
        if (page.scheme != "https" or page.hostname not in {"www.disneyplus.com", "disneyplus.com"}
                or not match):
            if not getattr(self, "reported_route", None) == page.path:
                LOG.info("Waiting for playback route: host=%s path=%s", page.hostname, page.path[:160])
                self.reported_route = page.path
            return {"cues": [], "status": {"state": "waiting", "message": "Open a Disney+ movie or episode."}}
        position = float(data.get("currentTime", 0))
        if not math.isfinite(position) or not 0 <= position <= MAX_TIME:
            raise ValueError("Invalid playback time")
        diagnostics = data.get("diagnostics", {})
        if not isinstance(diagnostics, dict):
            raise ValueError("Invalid diagnostics")
        observations = diagnostics.get("mediaRequests")
        if not isinstance(observations, list):
            with self.lock:
                self.track_ready = False
                if not getattr(self, "reported_old_client", False):
                    LOG.info("An older extension is connected; waiting for version 0.2.0 media evidence")
                    self.reported_old_client = True
            return {"cues": [], "status": {"state": "unsupported", "message": "Load extension version 0.2.0 and refresh Disney+ to identify each movie correctly."}}
        playback_id = match.group(1).lower()
        with self.lock:
            if playback_id != self.playback_id:
                self.generation += 1
                self.playback_id = playback_id
                self.movie_key = self.playlist_path = None
                self.store, self.completed, self.segments = {"clips": []}, set(), []
                self.track_ready = False
                self.state, self.message = "waiting", "Identifying this title’s German audio…"
            self.title = str(data.get("title", ""))[:250]
            self.media_requests = observations[:128]
            if time.monotonic() - self.last_sync > 15:
                LOG.info("Firefox playback connected at %.2fs", position)
            self.raw_position = position
            self.diagnostics = diagnostics
            clock = movie_clock(position, self.diagnostics)
            self.clock_ready = clock is not None
            if clock:
                self.position, self.clock_offset = clock
            self.last_sync = time.monotonic()
            result = self.snapshot()
        self.wake.set()
        self.gloss_wake.set()
        return result

    def activate_track(self, path):
        """Called under lock. Different edits/renditions keep independent timelines."""
        path = canonical_path(path)
        key = (self.legacy_key if self.playback_id == COCO_ID and path == self.legacy_path
               else self.playback_id + ":de:" + hashlib.sha256(path.encode()).hexdigest()[:16])
        if key != self.movie_key:
            self.generation += 1
            self.movie_key, self.playlist_path = key, path
            self.store = {"clips": self.db.load_clips(key)}
            self.completed = {canonical_path(k) for clip in self.store["clips"] for k in clip["keys"]}
            self.segments = []
            self.gloss_failed.clear()
            LOG.info("Selected German audio for playback %s (%d saved clips)", self.playback_id, len(self.store["clips"]))
        self.track_ready = True

    def gloss_profile(self):
        return GLOSS_PROFILE + ":" + (self.movie_key or "unknown")

    def cached_gloss(self, text):
        key = (self.gloss_profile(), text)
        if key not in self.gloss_cache:
            value = self.db.get_gloss(key[0], text)
            # Preserve paid-for Coco glosses. Improved guidance applies to new
            # captions; no background bulk reprocessing after a prompt revision.
            if value is None and self.movie_key == self.legacy_key:
                value = self.db.get_gloss("gpt-6.1-terra:de-A2-to-B1:en:v1", text)
            self.gloss_cache[key] = value
        return self.gloss_cache[key]

    def save_transcription(self, movie_key, record):
        """A request finishing after a movie switch must stay with its old movie."""
        saved = self.db.save_clip(movie_key, record)
        if movie_key == self.movie_key:
            if saved:
                self.store["clips"].append(record)
                self.completed.update(record["keys"])
            else:
                self.store["clips"] = self.db.load_clips(movie_key)
                self.completed = {canonical_path(k) for c in self.store["clips"] for k in c["keys"]}

    def snapshot(self, position=None):
        position = self.position if position is None else position
        cues = sorted((cue for clip in self.store["clips"] for cue in clip["cues"]
                       if cue["end"] >= position - 20 and cue["start"] < position + 120), key=lambda c: c["start"])
        transcribed_until = position
        for clip in sorted(self.store["clips"], key=lambda c: c["start"]):
            if clip["start"] <= transcribed_until + .12 and clip["end"] > transcribed_until:
                transcribed_until = clip["end"]
        offset = self.clock_offset
        shifted = [{**cue, "start": cue["start"] + offset, "end": cue["end"] + offset,
                    "text": (self.cached_gloss(cue["text"]) or cue["text"])
                    if self.config.get("glossary_enabled", True) else cue["text"]} for cue in cues]
        return {"cues": shifted if self.clock_ready and self.track_ready else [], "diagnostics": self.diagnostics,
                "status": {"state": self.state if self.clock_ready else "waiting",
                "message": self.message if self.clock_ready else "Play briefly to synchronize German subtitles…",
                "bufferedUntil": contiguous_end(self.segments, position) + offset if self.clock_ready else self.raw_position,
                "transcribedUntil": transcribed_until + offset if self.clock_ready else self.raw_position,
                "playbackTime": self.raw_position,
                "movieTime": self.position if self.clock_ready else None,
                "clockReady": self.clock_ready,
                "trackReady": self.track_ready,
                "playbackId": self.playback_id,
                "title": self.title,
                "clockOffset": offset if self.clock_ready else None,
                "connected": time.monotonic() - self.last_sync <= 15,
                "provider": "local-medium" if self.blocked and self.config.get("local_fallback") else "whisper-1",
                "glossary": dict(self.gloss_stats),
                "completedClips": len(self.store["clips"])}}

    def retry(self):
        with self.lock:
            self.blocked = False
            self.config["billing_paused"] = False
            save_json(CONFIG, self.config)
            self.retry_at = 0
            self.gloss_blocked = False
            self.gloss_failed.clear()
            self.gloss_retry_at = 0
            self.state, self.message = "waiting", "Preparing German subtitles…"
        self.wake.set()
        self.gloss_wake.set()

    def run(self):
        while True:
            self.wake.wait(2)
            self.wake.clear()
            with self.lock:
                if time.monotonic() - self.last_sync > 15 or not self.clock_ready:
                    continue
                position, generation = self.position, self.generation
                observations = list(self.media_requests)
            try:
                self.cache.refresh()
                path = select_audio_playlist(self.cache, observations)
                segments = cached_segments(self.cache, path)
                with self.lock:
                    if generation != self.generation:
                        continue
                    self.activate_track(path)
                    job_key = self.movie_key
                    self.segments = segments
                    if (self.blocked and not self.config.get("local_fallback")) or time.monotonic() < self.retry_at:
                        continue
                    clip, owned = choose_batch(segments, self.completed, position)
                    if not owned:
                        ready = self.snapshot(position)["status"]["transcribedUntil"] > position + self.clock_offset + .1
                        self.state = "ready" if ready else "waiting"
                        self.message = "German subtitles ready" if ready else "Waiting for German audio to buffer…"
                        continue
                    self.state, self.message = "preparing", "Preparing German subtitles…"
                started = time.monotonic()
                provider = "local-medium" if self.blocked else "whisper-1"
                with tempfile.TemporaryDirectory(prefix="audio-", dir=self.work_directory) as temporary:
                    wav, duration, audio_start = decode(clip, Path(temporary), owned[0].start)
                    if provider == "local-medium":
                        from local_transcribe import transcribe_local
                        transcript = transcribe_local(wav)
                    else:
                        transcript = transcribe(wav, self.config)
                    if transcript.get("silence"):
                        provider = "local-voice-check"
                cues = caption_cues(transcript, audio_start, owned[0].start, owned[-1].end)
                record = {"start": owned[0].start, "end": owned[-1].end,
                          "keys": [s.key for s in owned], "cues": cues,
                          "audio_seconds": duration, "model": provider}
                with self.lock:
                    self.save_transcription(job_key, record)
                    if job_key == self.movie_key:
                        self.state, self.message = "ready", "German subtitles ready"
                LOG.info("Transcribed %.2f–%.2fs (%d cues, %s) in %.1fs", record["start"], record["end"],
                         len(cues), provider, time.monotonic() - started)
                self.wake.set()
                self.gloss_wake.set()
            except AudioSelectionError as error:
                with self.lock:
                    if generation == self.generation:
                        self.track_ready = False
                        self.state, self.message = "waiting", str(error)
            except TranscriptionError as error:
                with self.lock:
                    self.state = "error"
                    if error.code in BILLING_CODES or error.code in {"invalid_api_key", "http_401", "http_403"}:
                        self.blocked = True
                        self.config["billing_paused"] = True
                        save_json(CONFIG, self.config)
                        self.message = ("OpenAI API credits are exhausted. Add credits, then retry."
                                        if error.code == "credit_balance_exhausted"
                                        else "OpenAI API billing or access needs attention. Fix it, then retry.")
                    else:
                        self.retry_at = time.monotonic() + 30
                        self.message = "Transcription is temporarily unavailable; retrying shortly."
                LOG.warning("Transcription stopped: %s", error.code)
            except (OSError, ValueError, RuntimeError, requests.RequestException, subprocess.SubprocessError) as error:
                with self.lock:
                    self.track_ready = False
                    self.state, self.message = "error", "Could not prepare audio. Check the local service log."
                    self.retry_at = time.monotonic() + 10
                LOG.warning("Audio preparation failed (%s)", type(error).__name__)

    def run_glossary(self):
        """Independent queue: enrichment can never block audio transcription/display."""
        while True:
            self.gloss_wake.wait(2)
            self.gloss_wake.clear()
            with self.lock:
                if (time.monotonic() - self.last_sync > 15 or not self.clock_ready or not self.track_ready or self.blocked
                        or self.gloss_blocked or time.monotonic() < self.gloss_retry_at
                        or not self.config.get("glossary_enabled", True)):
                    continue
                position = self.position
                cues = sorted((cue for clip in self.store["clips"] for cue in clip["cues"]
                               if cue["end"] > position + 1 and cue["start"] < position + 75),
                              key=lambda cue: (cue["start"] < position, cue["start"]))
                pending = list(dict.fromkeys(cue["text"] for cue in cues
                    if cue["text"] not in self.gloss_failed and self.cached_gloss(cue["text"]) is None))[:8]
                if not pending:
                    self.gloss_stats["state"] = "ready"
                    continue
                self.gloss_stats["state"] = "preparing"
                profile = self.gloss_profile()
                context = [cue["text"] for cue in sorted(
                    (cue for clip in self.store["clips"] for cue in clip["cues"]
                     if cue["end"] > position - 8 and cue["start"] < position + 75),
                    key=lambda cue: cue["start"])][:32]
            try:
                result = annotate(pending, read_key(self.config["key_file"]), context=context)
                for item in result["items"]:
                    self.db.put_gloss(profile, item["source"], item["text"],
                                      item["annotations"], result["latency_ms"])
                with self.lock:
                    self.gloss_cache.update({(profile, item["source"]): item["text"] for item in result["items"]})
                    self.gloss_stats["state"] = "ready"
                    self.gloss_stats["requests"] += 1
                    self.gloss_stats["inputTokens"] += result["input_tokens"]
                    self.gloss_stats["cachedInputTokens"] += result["cached_input_tokens"]
                    self.gloss_stats["outputTokens"] += result["output_tokens"]
                    self.gloss_stats["lastLatencyMs"] = result["latency_ms"]
                LOG.info("Terra glossed %d captions in %.2fs (%d input, %d cached input, %d output tokens)",
                         len(pending), result["latency_ms"] / 1000, result["input_tokens"],
                         result["cached_input_tokens"], result["output_tokens"])
                self.gloss_wake.set()
            except (GlossError, requests.RequestException, ValueError, OSError, KeyError) as error:
                with self.lock:
                    self.gloss_stats["state"] = "unavailable"
                    if profile == self.gloss_profile():
                        self.gloss_failed.update(pending)
                    self.gloss_retry_at = time.monotonic() + 30
                    if isinstance(error, GlossError) and str(error) in BILLING_CODES:
                        self.gloss_blocked = True
                LOG.warning("Glossary pass skipped (%s); original German remains available", type(error).__name__)


def handler_for(engine):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            # Host validation also rejects DNS-rebinding requests to the loopback socket.
            if self.headers.get("Host") != "127.0.0.1:8766":
                self.respond(403, {"error": "Invalid host"})
                return False
            expected = "Bearer " + engine.config["token"]
            if not secrets.compare_digest(self.headers.get("Authorization", ""), expected):
                self.respond(401, {"error": "Local authorization required"})
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            if self.path != "/status":
                self.respond(404, {"error": "Not found"})
                return
            with engine.lock:
                self.respond(200, engine.snapshot())

        def do_POST(self):
            if not self.authorized():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 300000:
                    raise ValueError("Invalid request size")
                data = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("Invalid JSON object")
                if self.path == "/sync":
                    self.respond(200, engine.sync(data))
                elif self.path == "/retry":
                    engine.retry()
                    self.respond(200, {"ok": True})
                else:
                    self.respond(404, {"error": "Not found"})
            except (ValueError, TypeError):
                self.respond(400, {"error": "Invalid request"})
    return Handler


def setup(cache_directory=None, key_file=None):
    if CONFIG.exists():
        config = json.loads(CONFIG.read_text())
    else:
        if cache_directory is None:
            bases = [Path.home() / "snap/firefox/common/.cache/mozilla/firefox",
                     Path.home() / ".cache/mozilla/firefox"]
            directories = [p for base in bases for p in base.glob("*/cache2/entries")]
            if len(directories) != 1:
                raise SystemExit("Specify your active Firefox cache2/entries directory with --cache-directory.")
            cache_directory = directories[0]
        if not Path(cache_directory).expanduser().is_dir():
            raise SystemExit("Firefox cache directory does not exist. Play briefly in Firefox first.")
        config = {"cache_directory": str(Path(cache_directory).expanduser().resolve()),
                  "key_file": str(Path(key_file).expanduser().resolve()) if key_file else str(CONFIG_DIR / "openai-key.txt"),
                  "token": secrets.token_urlsafe(32), "billing_paused": False, "local_fallback": False}
    if cache_directory:
        if not Path(cache_directory).expanduser().is_dir():
            raise SystemExit("Firefox cache directory does not exist.")
        config["cache_directory"] = str(Path(cache_directory).expanduser().resolve())
    if key_file:
        config["key_file"] = str(Path(key_file).expanduser().resolve())
    save_json(CONFIG, config)
    pairing = CONFIG.parent / "pairing-code.txt"
    pairing.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pairing.write_text(config["token"] + "\n")
    pairing.chmod(0o600)
    print(f"Configuration: {CONFIG}\nPairing code: {pairing}\nNo credentials are bundled in the extension.")


def main():
    global CONFIG
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["setup", "serve", "inspect"], nargs="?", default="serve")
    parser.add_argument("--time", type=float, default=838)
    parser.add_argument("--playlist", help="Canonical cached audio playlist path for read-only inspection")
    parser.add_argument("--config", type=Path, default=CONFIG, help="Private service configuration file")
    parser.add_argument("--cache-directory", type=Path, help="Active Firefox profile cache2/entries directory (setup)")
    parser.add_argument("--key-file", type=Path, help="File containing your OpenAI API key (setup)")
    args = parser.parse_args()
    CONFIG = args.config.expanduser()
    if args.command == "setup":
        setup(args.cache_directory, args.key_file)
        return
    config = json.loads(CONFIG.read_text())
    if args.command == "inspect":
        cache = FirefoxCache(config["cache_directory"])
        cache.refresh()
        segments = cached_segments(cache, args.playlist or config.get("audio_playlist_path") or cache.latest_german_playlist())
        print(json.dumps({"cachedGermanSegments": len(segments), "position": args.time,
                          "bufferedUntil": contiguous_end(segments, args.time)}, indent=2))
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    engine = Engine(config)
    server = ThreadingHTTPServer(("127.0.0.1", 8766), handler_for(engine))
    threading.Thread(target=engine.run, daemon=True).start()
    threading.Thread(target=engine.run_glossary, daemon=True).start()
    LOG.info("Subtitle service listening on 127.0.0.1:8766")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
