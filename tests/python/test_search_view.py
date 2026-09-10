import unittest

from services.orchestration.app.retrieval.search_view import (
    compact_text,
    escape_like,
    result_count,
    search_snippet,
)


class SearchProjectionTests(unittest.TestCase):
    def test_escape_like_treats_user_wildcards_as_literals(self):
        self.assertEqual(escape_like(r"50%_done\\now"), r"50\%\_done\\\\now")

    def test_snippet_is_compact_and_centered_near_a_match(self):
        text = "prefix " * 30 + "Project Borealis launch protocol" + " suffix" * 30
        snippet = search_snippet(text, "Borealis", max_length=90)
        self.assertLessEqual(len(snippet), 92)
        self.assertIn("Borealis", snippet)
        self.assertTrue(snippet.startswith("…"))
        self.assertTrue(snippet.endswith("…"))

    def test_compact_text_and_empty_counts_are_stable(self):
        self.assertEqual(compact_text(" one\n\t two  "), "one two")
        self.assertEqual(result_count([]), 0)
        self.assertEqual(result_count([{"total_matches": 12}]), 12)


if __name__ == "__main__":
    unittest.main()

