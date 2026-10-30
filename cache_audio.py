"""Read only the clear German audio already present in Firefox's HTTP cache."""
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit
import gzip
import math
import re
import struct


def canonical_path(value):
    """Disney changes its signed leading path component on player refresh."""
    path = urlsplit(value).path
    parts = path.split("/")
    if len(parts) > 2 and len(parts[1]) >= 128:
        return "/" + "/".join(parts[2:])
    return path


def boxes(data, start=0, end=None):
    end = len(data) if end is None else end
    while start + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, start)
        header = 8
        if size == 1:
            size = struct.unpack_from(">Q", data, start + 8)[0]
            header = 16
        elif size == 0:
            size = end - start
        if size < header or start + size > end:
            raise ValueError("Incomplete MP4 box")
        yield kind, start + header, start + size
        start += size


def find_box(data, path):
    def descend(start, end, remaining):
        for kind, body, limit in boxes(data, start, end):
            if kind == remaining[0]:
                if len(remaining) == 1:
                    return data[body:limit]
                found = descend(body, limit, remaining[1:])
                if found is not None:
                    return found
        return None
    return descend(0, len(data), path)


def timescale(init):
    mdhd = find_box(init, [b"moov", b"trak", b"mdia", b"mdhd"])
    if mdhd is None:
        raise ValueError("Audio initialization has no timescale")
    return struct.unpack_from(">I", mdhd, 20 if mdhd[0] == 1 else 12)[0]


def media_time(fragment, scale):
    tfdt = find_box(fragment, [b"moof", b"traf", b"tfdt"])
    if tfdt is None:
        raise ValueError("Audio fragment has no timestamp")
    return struct.unpack_from(">Q" if tfdt[0] == 1 else ">I", tfdt, 4)[0] / scale


def attributes(line):
    return {k: quoted or plain
            for k, quoted, plain in re.findall(r'([A-Z0-9-]+)=(?:"([^"]*)"|([^,]*))', line)}


@dataclass
class Entry:
    file: Path
    size: int
    url: str
    encoding: str
    modified: float

    @property
    def path(self):
        return urlsplit(self.url).path

    def read(self):
        with self.file.open("rb") as stream:
            data = stream.read(self.size)
        if len(data) != self.size:
            raise ValueError("Cache entry changed while reading")
        if self.encoding == "gzip":
            return gzip.decompress(data)
        if self.encoding:
            raise ValueError("Unsupported cache content encoding")
        return data


class FirefoxCache:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.fingerprints = {}
        self.entries = {}
        self.timeline_cache = {}

    def refresh(self):
        seen = set()
        for file in self.directory.iterdir():
            try:
                stat = file.stat()
                if not file.is_file():
                    continue
                seen.add(file.name)
                fingerprint = (stat.st_mtime_ns, stat.st_size)
                if self.fingerprints.get(file.name) == fingerprint:
                    continue
                self.entries.pop(file.name, None)
                if stat.st_size < 8:
                    continue
                with file.open("rb") as stream:
                    stream.seek(-4, 2)
                    size = struct.unpack(">I", stream.read(4))[0]
                    if not 0 <= size < stat.st_size or stat.st_size - size > 262144:
                        continue
                    stream.seek(size)
                    metadata = stream.read()
                # Read only cache metadata until the media CDN and type are identified.
                match = re.search(rb'https?://[^\x00\s]+', metadata)
                content_type = re.search(rb'content-type:\s*([^\r\n\x00]+)', metadata, re.I)
                self.fingerprints[file.name] = fingerprint
                if not match or not content_type:
                    continue
                url = match.group().decode("utf-8", "replace")
                if not (urlsplit(url).hostname or "").endswith(".media.dssott.com"):
                    continue
                kind = content_type.group(1).lower()
                if b"audio" not in kind and b"mpegurl" not in kind:
                    continue
                encoding = re.search(rb'content-encoding:\s*([^\r\n\x00]+)', metadata, re.I)
                self.entries[file.name] = Entry(file, size, url,
                    encoding.group(1).decode().lower() if encoding else "", stat.st_mtime)
            except (OSError, ValueError, struct.error):
                # In-progress writes are retried on the next scan.
                self.fingerprints.pop(file.name, None)
        for name in set(self.fingerprints) - seen:
            self.fingerprints.pop(name, None)
            self.entries.pop(name, None)

    def by_path(self):
        result = {}
        for entry in sorted(self.entries.values(), key=lambda e: e.modified):
            result[canonical_path(entry.path)] = entry
        return result

    def latest_german_playlist(self):
        for entry in sorted(self.entries.values(), key=lambda e: e.modified, reverse=True):
            if not entry.path.endswith(".m3u8"):
                continue
            try:
                for line in entry.read().decode().splitlines():
                    if not line.startswith("#EXT-X-MEDIA:"):
                        continue
                    values = attributes(line)
                    if (values.get("TYPE") == "AUDIO" and values.get("LANGUAGE") == "de"
                            and values.get("NAME") == "German" and values.get("GROUP-ID") == "aac-128k"):
                        return urlsplit(urljoin(entry.url, values["URI"])).path
            except (OSError, ValueError, UnicodeError):
                continue
        raise ValueError("Play a Disney+ title with German audio so its playlist enters Firefox's cache.")


class AudioSelectionError(ValueError):
    """A recoverable playback/track selection state suitable for the popup."""


def select_audio_playlist(cache, observations):
    """Associate only audio fetched by this player with a cached HLS language tag.

    Never choose the newest global cache entry: other tabs, trailers and previous
    movies can all leave valid German playlists there. An actual rendition/init/
    segment request must match; a master manifest alone is not enough evidence.
    """
    observed = {}
    for item in observations[:128]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        path, stamp = item["path"], item.get("observedAt")
        if (not path.startswith("/") or len(path) > 2048 or "?" in path or "#" in path
                or isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp)):
            continue
        path = canonical_path(path)
        observed[path] = max(stamp, observed.get(path, 0))
    if not observed:
        raise AudioSelectionError("Play briefly with German audio to identify this title’s track…")
    entries = cache.by_path()
    candidates = {}
    for master in entries.values():
        if not master.path.endswith(".m3u8"):
            continue
        try:
            lines = master.read().decode().splitlines()
        except (OSError, ValueError, UnicodeError):
            continue
        for line in lines:
            if not line.startswith("#EXT-X-MEDIA:"):
                continue
            attrs = attributes(line)
            if attrs.get("TYPE") != "AUDIO" or not attrs.get("URI"):
                continue
            path = canonical_path(urljoin(master.url, attrs["URI"]))
            playlist = entries.get(path)
            if playlist is None:
                continue
            try:
                media_lines = playlist.read().decode().splitlines()
                references = {path}
                for media_line in media_lines:
                    if media_line.startswith("#EXT-X-MAP:"):
                        uri = attributes(media_line).get("URI")
                    else:
                        uri = media_line if media_line and not media_line.startswith("#") else None
                    if uri:
                        references.add(canonical_path(urljoin(playlist.url, uri)))
            except (OSError, ValueError, UnicodeError):
                continue
            # A freshly fetched media segment is stronger evidence than speculative
            # playlist prefetch. Keep track switches detectable even while paused.
            media_hits = [observed[p] for p in references - {path} if p in observed]
            if media_hits:
                score = (2, max(media_hits))
            elif path in observed:
                score = (1, observed[path])
            else:
                continue
            candidates[path] = (score, attrs)
    if not candidates:
        raise AudioSelectionError("Waiting for this player’s audio playlist in Firefox’s cache…")
    best_score = max(score for score, _ in candidates.values())
    best = [(path, attrs) for path, (score, attrs) in candidates.items() if score == best_score]
    if len(best) != 1:
        raise AudioSelectionError("Waiting for an unambiguous audio track…")
    path, attrs = best[0]
    if attrs.get("LANGUAGE", "").lower().split("-")[0] != "de":
        raise AudioSelectionError("Select German audio in Disney+ to use German learning subtitles.")
    if ("describes-video" in attrs.get("CHARACTERISTICS", "")
            or re.search(r"description|deskription|\bAD\b", attrs.get("NAME", ""), re.I)):
        raise AudioSelectionError("Select German audio without audio description in Disney+.")
    return path


@dataclass
class Segment:
    entry: Entry
    start: float
    duration: float
    init: Entry

    @property
    def end(self):
        return self.start + self.duration

    @property
    def key(self):
        return canonical_path(self.entry.path)


def cached_segments(cache, playlist_path):
    entries = cache.by_path()
    playlist = entries.get(canonical_path(playlist_path))
    if not playlist:
        return []
    result = []
    init = None
    scale = None
    duration = None
    encrypted = False
    for line in playlist.read().decode().splitlines():
        if line.startswith("#EXT-X-KEY:"):
            encrypted = attributes(line).get("METHOD") != "NONE"
        elif line.startswith("#EXT-X-MAP:"):
            init = entries.get(canonical_path(urljoin(playlist.url, attributes(line)["URI"])))
            if init:
                data = init.read()
                if b"enca" in data or b"tenc" in data:
                    raise AudioSelectionError("This audio track is encrypted; buffered transcription is unavailable for this rendition.")
                scale = timescale(data)
        elif line.startswith("#EXTINF:"):
            duration = float(line.partition(":")[2].split(",", 1)[0])
        elif line and not line.startswith("#") and duration is not None:
            entry = entries.get(canonical_path(urljoin(playlist.url, line)))
            if entry and init and scale:
                if encrypted:
                    raise AudioSelectionError("This audio track is encrypted; buffered transcription is unavailable for this rendition.")
                try:
                    signature = (entry.file.name, entry.size, entry.modified, scale)
                    start = cache.timeline_cache.get(signature)
                    if start is None:
                        start = media_time(entry.read(), scale)
                        cache.timeline_cache[signature] = start
                    if math.isfinite(start) and 0 < duration <= 30:
                        result.append(Segment(entry, start, duration, init))
                except (OSError, ValueError, struct.error):
                    pass
            duration = None
    return sorted(result, key=lambda s: s.start)


def contiguous_end(segments, position):
    end = position
    for segment in segments:
        if segment.start <= end + 0.12 and segment.end > end:
            end = segment.end
    return end
