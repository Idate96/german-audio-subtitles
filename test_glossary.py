import unittest
from glossary import insert_glosses


class GlossTests(unittest.TestCase):
    def test_original_words_order_and_punctuation_are_preserved(self):
        text = "Die Blütenblätter führen unsere Vorfahren nach Hause."
        result = insert_glosses(text, [{"german": "Blütenblätter", "english": "petals"},
                                      {"german": "Vorfahren", "english": "ancestors"}])
        self.assertEqual(result, "Die Blütenblätter (petals) führen unsere Vorfahren (ancestors) nach Hause.")

    def test_nonexistent_words_and_partial_word_matches_are_ignored(self):
        self.assertEqual(insert_glosses("Er lebt hier.", [
            {"german": "Leben", "english": "life"}, {"german": "leb", "english": "live"}]), "Er lebt hier.")

    def test_simple_captions_are_cached_unchanged(self):
        self.assertEqual(insert_glosses("Hallo, Miguel!", []), "Hallo, Miguel!")

    def test_overlapping_and_unbounded_glosses_are_rejected(self):
        self.assertEqual(insert_glosses("nach Hause", [{"german": "nach Hause", "english": "home"},
            {"german": "Hause", "english": "house"}]), "nach Hause (home)")
        self.assertEqual(insert_glosses("Hause", [{"german": "Hause", "english": "<script>"}]), "Hause")


if __name__ == "__main__":
    unittest.main()
