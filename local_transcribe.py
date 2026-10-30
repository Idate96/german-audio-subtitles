"""Local fallback using the installed Whisper model; no audio leaves the machine."""
import subprocess
import wave

_model = None


def speech_intervals(wav):
    """High-recall local voice check; does not load the Whisper model or call an API."""
    import numpy as np
    from faster_whisper.vad import get_speech_timestamps, VadOptions
    with wave.open(str(wav)) as stream:
        if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (16000, 1, 2):
            raise ValueError("Voice check requires decoded 16 kHz mono PCM")
        audio = np.frombuffer(stream.readframes(stream.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    regions = get_speech_timestamps(audio, VadOptions(threshold=.25, min_speech_duration_ms=100,
        min_silence_duration_ms=200, speech_pad_ms=300))
    return [(region["start"] / 16000, region["end"] / 16000) for region in regions]


def transcribe_local(wav):
    import numpy as np
    from faster_whisper import WhisperModel

    global _model
    if _model is None:
        _model = WhisperModel("medium", device="cpu", compute_type="int8", cpu_threads=4,
                              local_files_only=True)
    # Decode explicitly to avoid depending on the installed PyAV file API.
    audio = np.frombuffer(subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", str(wav), "-f", "f32le", "-ac", "1", "-ar", "16000", "-"
    ]), dtype=np.float32)
    segments, _ = _model.transcribe(audio, language="de", beam_size=5, word_timestamps=True,
        vad_filter=True, vad_parameters={"threshold": .25, "speech_pad_ms": 300},
        initial_prompt="Deutsche Filmsprache. Gesprochene Wörter und Namen wortgetreu beibehalten.")
    result = {"segments": [], "words": []}
    for segment in segments:
        result["segments"].append({"start": float(segment.start), "end": float(segment.end),
            "text": segment.text, "no_speech_prob": segment.no_speech_prob})
        result["words"].extend({"start": float(w.start), "end": float(w.end), "word": w.word}
                               for w in segment.words or [])
    return result
