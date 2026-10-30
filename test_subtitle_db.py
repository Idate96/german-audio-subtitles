from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from subtitle_db import SubtitleDB


def clip(start=840, keys=None, text="Hallo, Miguel!"):
    return {
        "start": start, "end": start + 8, "keys": keys or [f"segment-{start}"],
        "cues": [{"start": start + 0.5, "end": start + 2.5, "text": text}],
        "audio_seconds": 8.0, "model": "whisper-1",
    }


class SubtitleDBTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "subtitles.sqlite3"
        self.db = SubtitleDB(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def test_replay_is_idempotent_and_persists_originals_after_reopen(self):
        original = clip()
        self.assertTrue(self.db.save_clip("coco:german-aac", original))
        self.assertFalse(self.db.save_clip("coco:german-aac", original))
        original["cues"][0]["text"] = "Caller edited its own object"
        reopened = SubtitleDB(self.path)
        self.assertEqual(reopened.load_clips("coco:german-aac"), [clip()])
        self.assertEqual(reopened.counts(), {"clips": 1, "cues": 1, "segments": 1, "glosses": 0})
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_overlapping_batch_is_atomic_and_never_overwrites_original(self):
        self.assertTrue(self.db.save_clip("coco", clip(keys=["a", "b"])))
        self.assertFalse(self.db.save_clip("coco", clip(848, ["b", "c"], "Replacement")))
        self.assertEqual(self.db.counts()["segments"], 2)
        self.assertTrue(self.db.save_clip("coco", clip(848, ["c"])))
        records = self.db.load_clips("coco")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["cues"][0]["text"], "Hallo, Miguel!")

    def test_movie_and_track_namespaces_do_not_collide(self):
        for movie in ("coco:de-aac", "coco:en-aac", "other-movie:de-aac"):
            self.assertTrue(self.db.save_clip(movie, clip(keys=["same-segment-name"], text=movie)))
        self.assertEqual(self.db.counts()["clips"], 3)
        self.assertEqual(self.db.load_clips("coco:de-aac")[0]["cues"][0]["text"], "coco:de-aac")
        self.assertEqual(self.db.counts("coco:en-aac")["segments"], 1)

    def test_migration_preserves_file_and_existing_records(self):
        self.db.save_clip("coco", clip(840, ["first"], "Keep original"))
        source = Path(self.directory.name) / "transcripts.json"
        source.write_text(json.dumps({"clips": [
            clip(848, ["second"], "Die Grüße"), clip(840, ["first"], "Do not overwrite")
        ]}, ensure_ascii=False), encoding="utf-8")
        before = source.read_bytes()
        self.assertEqual(self.db.import_json("coco", source), 1)
        self.assertEqual(self.db.import_json("coco", source), 0)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual([r["cues"][0]["text"] for r in self.db.load_clips("coco")], ["Keep original", "Die Grüße"])
        self.assertEqual(self.db.import_json("coco", source.with_name("absent.json")), 0)

    def test_invalid_migration_does_not_partially_import(self):
        source = Path(self.directory.name) / "bad.json"
        source.write_text(json.dumps({"clips": [clip(), {"start": 0}]}))
        with self.assertRaises(ValueError):
            self.db.import_json("coco", source)
        self.assertEqual(self.db.counts()["clips"], 0)

    def test_gloss_cache_preserves_exact_source_and_profile_including_unchanged_text(self):
        source = "Ich erinnere mich."
        enhanced = "Ich erinnere mich (I remember)."
        self.db.put_gloss("terra:A2-B1:v1", source, enhanced, [{"word": "erinnere", "gloss": "remember"}], 91.5)
        self.db.put_gloss("terra:A2-B1:v2", source, source)
        self.db.put_gloss("terra:A2-B1:v1", " "+source, "A separate exact sentence")
        reopened = SubtitleDB(self.path)
        self.assertEqual(reopened.get_gloss("terra:A2-B1:v1", source), enhanced)
        self.assertEqual(reopened.get_gloss("terra:A2-B1:v2", source), source)
        self.assertIsNone(reopened.get_gloss("terra:B2:v1", source))
        self.assertIsNone(reopened.get_gloss("terra:A2-B1:v1", source.lower()))
        self.assertEqual(reopened.counts(profile_key="terra:A2-B1:v1")["glosses"], 2)
        with sqlite3.connect(self.path) as connection:
            row = connection.execute("SELECT source_text,annotations_json,latency_ms FROM glosses WHERE profile_key=? AND source_text=?",
                                     ("terra:A2-B1:v1", source)).fetchone()
            self.assertEqual(row[0], source)
            self.assertEqual(json.loads(row[1])[0]["gloss"], "remember")
            self.assertEqual(row[2], 91.5)

    def test_concurrent_readers_writers_and_duplicate_claims(self):
        def work(index):
            saved = self.db.save_clip("coco", clip(index // 2 * 8, [f"shared-{index // 2}"]))
            self.db.put_gloss("profile", f"Satz {index}", f"Satz {index} (sentence)")
            self.db.load_clips("coco")
            self.db.counts("coco", "profile")
            return saved

        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(work, range(40)))
        self.assertEqual(sum(outcomes), 20)
        reopened = SubtitleDB(self.path)
        self.assertEqual(reopened.counts(), {"clips": 20, "cues": 20, "segments": 20, "glosses": 40})
        self.assertEqual(reopened.get_gloss("profile", "Satz 39"), "Satz 39 (sentence)")


if __name__ == "__main__":
    unittest.main()
