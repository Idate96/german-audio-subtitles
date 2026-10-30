import http.client
from http.server import ThreadingHTTPServer
import json
from types import SimpleNamespace
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cache_audio import (Segment, attributes, contiguous_end, canonical_path,
                         select_audio_playlist, AudioSelectionError)
from server import caption_cues, choose_batch, handler_for, movie_clock, Engine, filter_nonspeech, transcribe


def segment(start, duration=8):
    return Segment(SimpleNamespace(path=str(start)), start, duration, SimpleNamespace(path="init"))


class TimelineTests(unittest.TestCase):
    def test_music_only_segments_do_not_invent_caption_credits(self):
        raw = {"segments": [{"start": 1, "end": 2, "text": "Hallo!"},
                            {"start": 8, "end": 9, "text": "Untertitel der Amara org Community"}],
               "words": [{"word": "Hallo!", "start": 1, "end": 2},
                         {"word": "Untertitel", "start": 8, "end": 8.5}]}
        filtered = filter_nonspeech(raw, [(0.8, 2.2)])
        self.assertEqual(filtered["text"], "Hallo!")
        self.assertEqual(filtered["words"], [raw["words"][0]])
        self.assertEqual(len(raw["segments"]), 2)  # original response stays intact

    def test_no_detected_voice_skips_paid_request(self):
        with patch("server.speech_intervals", return_value=[]), patch("server.requests.post") as api:
            result = transcribe("unused.wav", {})
        api.assert_not_called()
        self.assertFalse(result["words"])

    def test_refreshed_signed_urls_reuse_same_audio_identifiers(self):
        first = "/" + "a" * 295 + "/coco/audio/de/pts_123.mp4a"
        second = "/" + "b" * 295 + "/coco/audio/de/pts_123.mp4a"
        self.assertEqual(canonical_path(first), canonical_path(second))
        self.assertEqual(canonical_path("/coco/audio/de/pts_123.mp4a"), canonical_path(first))

    def test_mse_clock_mapping_uses_observed_offset_not_assumed_movie_clock(self):
        self.assertEqual(movie_clock(38.485, {"mseTimestampOffset": -800}), (838.485, -800))
        self.assertEqual(movie_clock(838, {"mseTimestampOffset": 0}), (838, 0))
        self.assertIsNone(movie_clock(38, {}))
        self.assertIsNone(movie_clock(38, {"mseTimestampOffset": float("nan")}))

    def test_submillisecond_av_drift_does_not_disable_captions_after_seek(self):
        evidence = [{"offset": -5192, "eligible": True, "association": "fresh-context", "kind": "video"},
                    {"offset": -5192.001, "eligible": True, "association": "fresh-context", "kind": "unknown"}]
        self.assertEqual(movie_clock(42, {"mseBuffers": evidence}), (5234, -5192))
        evidence[1]["offset"] = -804.824
        self.assertIsNone(movie_clock(42, {"mseBuffers": evidence}))
        evidence[1]["offset"] = -5192
        evidence[1]["eligible"] = False
        self.assertIsNone(movie_clock(42, {"mseBuffers": evidence}))

    def test_hls_attributes_keep_unquoted_type_and_quoted_language(self):
        a = attributes('#EXT-X-MEDIA:TYPE=AUDIO,NAME="German",LANGUAGE="de",URI="a,b.m3u8"')
        self.assertEqual(a, {"TYPE": "AUDIO", "NAME": "German", "LANGUAGE": "de", "URI": "a,b.m3u8"})

    def test_buffer_stops_at_gap_instead_of_claiming_unplayed_future_is_available(self):
        self.assertEqual(contiguous_end([segment(832), segment(840), segment(880)], 838), 848)

    def test_seek_prioritizes_new_position_and_does_not_rebill_completed_segments(self):
        segments = [segment(x) for x in [0, 8, 16, 32, 40, 48, 56, 64, 72, 80]]
        clip, owned = choose_batch(segments, {"40", "48"}, 45)
        self.assertEqual([s.start for s in owned], [56, 64, 72, 80])
        self.assertEqual(clip[0].start, 48)  # context only, not a second subtitle ownership interval
        self.assertEqual(choose_batch(segments, {str(s.start) for s in segments}, 45), ([], []))

    def test_clip_never_concatenates_across_missing_audio(self):
        clip, owned = choose_batch([segment(0), segment(8), segment(32)], set(), 1)
        self.assertEqual([s.start for s in owned], [0, 8])

    def test_word_timestamps_are_relative_to_decoded_clip_not_current_playhead(self):
        response = {"words": [{"start": 1, "end": 2, "word": "Hallo"},
                              {"start": 2.1, "end": 3, "word": "Miguel."}]}
        self.assertEqual(caption_cues(response, 840, 840, 848),
                         [{"start": 841.0, "end": 843.0, "text": "Hallo Miguel."}])

    def test_transcript_punctuation_is_kept_with_word_times(self):
        response = {"text": "Hallo, Miguel!", "words": [
            {"start": 0, "end": .4, "word": "Hallo"}, {"start": .5, "end": 1.2, "word": "Miguel"}]}
        self.assertEqual(caption_cues(response, 0, 0, 8)[0]["text"], "Hallo, Miguel!")

    def test_zero_width_short_words_are_not_deleted_from_dialogue(self):
        response = {"text": "Ich glaube, wir sind die einzige Familie.", "words": [
            {"start": 0, "end": .3, "word": "Ich"}, {"start": .3, "end": .8, "word": "glaube"},
            {"start": .8, "end": .8, "word": "wir"}, {"start": .8, "end": 1, "word": "sind"},
            {"start": 1, "end": 1.1, "word": "die"}, {"start": 1.1, "end": 1.7, "word": "einzige"},
            {"start": 1.7, "end": 2.2, "word": "Familie"}]}
        self.assertEqual(caption_cues(response, 0, 0, 8)[0]["text"], response["text"])

    def test_long_movies_are_supported(self):
        self.assertEqual(movie_clock(100, {"mseTimestampOffset": -10000}), (10100, -10000))

    def test_overlap_context_does_not_duplicate_previous_captions(self):
        response = {"words": [{"start": 1, "end": 2, "word": "vorher"},
                              {"start": 8, "end": 9, "word": "jetzt"}]}
        self.assertEqual(caption_cues(response, 832, 840, 848),
                         [{"start": 840.0, "end": 841.0, "text": "jetzt"}])

    def test_silence_is_not_covered_by_one_long_caption(self):
        response = {"words": [{"start": 0, "end": 1, "word": "Hallo"},
                              {"start": 4, "end": 5, "word": "Miguel"}]}
        self.assertEqual(len(caption_cues(response, 0, 0, 8)), 2)


class TrackSelectionTests(unittest.TestCase):
    def setUp(self):
        self.entries = {}
        def add(path, data):
            self.entries[path] = SimpleNamespace(path=path, url="https://x.media.dssott.com"+path,
                                                  read=lambda: data.encode())
        add("/movie-a/master.m3u8", '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,LANGUAGE="de-DE",NAME="Deutsch",URI="de.m3u8"\n'
            '#EXT-X-MEDIA:TYPE=AUDIO,LANGUAGE="en",NAME="English",URI="en.m3u8"')
        add("/movie-b/master.m3u8", '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,LANGUAGE="de",NAME="German",URI="de.m3u8"')
        for movie, language in [("a", "de"), ("a", "en"), ("b", "de")]:
            add(f"/movie-{movie}/{language}.m3u8", f'#EXTM3U\n#EXT-X-MAP:URI="{language}-init.mp4"\n#EXTINF:8,\n{language}-1.mp4a')
        self.cache = SimpleNamespace(by_path=lambda: self.entries)

    def test_match_this_tabs_segment_not_another_cached_movie(self):
        observations = [{"path": "/movie-b/de-1.mp4a", "observedAt": 123}]
        self.assertEqual(select_audio_playlist(self.cache, observations), "/movie-b/de.m3u8")

    def test_language_switch_does_not_keep_old_german_subtitles(self):
        with self.assertRaisesRegex(AudioSelectionError, "Select German"):
            select_audio_playlist(self.cache, [{"path":"/movie-a/de-1.mp4a", "observedAt":123},
                                               {"path":"/movie-a/en-1.mp4a", "observedAt":124}])

    def test_master_alone_cannot_establish_selected_audio(self):
        with self.assertRaises(AudioSelectionError):
            select_audio_playlist(self.cache, [{"path":"/movie-a/master.m3u8", "observedAt":123}])

    def test_equal_conflicting_evidence_fails_closed(self):
        with self.assertRaisesRegex(AudioSelectionError, "unambiguous"):
            select_audio_playlist(self.cache, [{"path":"/movie-a/de-1.mp4a", "observedAt":123},
                                               {"path":"/movie-b/de-1.mp4a", "observedAt":123}])


class MovieSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch("server.ROOT", Path(self.temp.name)); self.patch.start()
        self.engine = Engine({"cache_directory":self.temp.name, "glossary_enabled":False,
                              "state_directory":self.temp.name, "work_directory":self.temp.name})

    def tearDown(self):
        self.patch.stop(); self.temp.cleanup()

    def connect(self, movie):
        return self.engine.sync({"pageUrl":"https://www.disneyplus.com/de-de/play/"+movie,
            "currentTime":1, "diagnostics":{"mseTimestampOffset":0,
                "mediaRequests":[{"path":"/"+movie+"/de-1.mp4a", "observedAt":123}]}})

    def record(self):
        return {"start":0,"end":8,"keys":["/a/segment"],"cues":[{"start":0,"end":3,"text":"Hallo"}],
                "audio_seconds":8,"model":"test"}

    def test_movie_switch_clears_captions_before_new_audio_is_resolved(self):
        self.connect("movie-aaaaaaaa"); self.engine.activate_track("/a/de.m3u8")
        self.engine.save_transcription(self.engine.movie_key,self.record())
        self.assertTrue(self.engine.snapshot()["cues"])
        response = self.connect("movie-bbbbbbbb")
        self.assertFalse(response["cues"])
        self.assertFalse(response["status"]["trackReady"])

    def test_old_request_finishing_after_switch_saves_only_to_old_movie(self):
        self.connect("movie-aaaaaaaa"); self.engine.activate_track("/a/de.m3u8")
        old_key=self.engine.movie_key
        self.connect("movie-bbbbbbbb"); self.engine.activate_track("/b/de.m3u8")
        self.engine.save_transcription(old_key,self.record())
        self.assertFalse(self.engine.snapshot()["cues"])
        self.assertEqual(len(self.engine.db.load_clips(old_key)),1)
        self.connect("movie-aaaaaaaa"); self.engine.activate_track("/a/de.m3u8")
        self.assertEqual(len(self.engine.snapshot()["cues"]),1)

    def test_different_rendition_cannot_reuse_wrong_timeline(self):
        self.connect("movie-aaaaaaaa"); self.engine.activate_track("/a/de.m3u8")
        self.engine.save_transcription(self.engine.movie_key,self.record())
        self.engine.activate_track("/a/other-cut-de.m3u8")
        self.assertFalse(self.engine.snapshot()["cues"])

    def test_old_extension_does_not_show_coco_over_another_movie(self):
        result=self.engine.sync({"pageUrl":"https://www.disneyplus.com/play/movie-aaaaaaaa","currentTime":1})
        self.assertEqual(result["status"]["state"],"unsupported")
        self.assertFalse(result["cues"])


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.engine = SimpleNamespace(config={"token": "test-only"}, lock=threading.RLock(),
                                      snapshot=lambda: {"cues": [], "status": {"state": "waiting"}})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.engine))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, token=None, host="127.0.0.1:8766"):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        headers = {"Host": host}
        if token:
            headers["Authorization"] = "Bearer " + token
        conn.request("GET", "/status", headers=headers)
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    def test_missing_or_wrong_token_is_denied(self):
        self.assertEqual(self.request()[0], 401)
        self.assertEqual(self.request("wrong")[0], 401)
        self.assertEqual(self.request("test-only")[0], 200)

    def test_foreign_host_is_denied_even_with_token(self):
        self.assertEqual(self.request("test-only", "elsewhere.example")[0], 403)


if __name__ == "__main__":
    unittest.main()
