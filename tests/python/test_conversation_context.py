import unittest

from services.orchestration.app.conversation import (
    MAX_CONTEXT_BYTES,
    MAX_CONTEXT_TURNS,
    MAX_QUESTION_BYTES,
    MAX_RESPONSE_BYTES,
    build_conversation_context,
    contextualize_question,
    render_conversation_context,
    validate_conversation_context,
)


class ConversationContextTests(unittest.TestCase):
    def test_context_is_bounded_ordered_and_hash_validated(self):
        rows = [
            {
                "id": f"run-{index}",
                "input_query": f"question-{index}-" + ("😀" * 1_000),
                "output_response": f"response-{index}-" + ("Δ" * 3_000),
                "answer_status": "grounded",
            }
            for index in range(8)
        ]

        context = build_conversation_context(rows)

        self.assertLessEqual(context["turn_count"], MAX_CONTEXT_TURNS)
        self.assertEqual(context["turns"][-1]["run_id"], "run-7")
        self.assertLessEqual(
            len(context["turns"][-1]["question"].encode("utf-8")),
            MAX_QUESTION_BYTES,
        )
        self.assertLessEqual(
            len(context["turns"][-1]["response"].encode("utf-8")),
            MAX_RESPONSE_BYTES,
        )
        self.assertLessEqual(
            len(str(context).encode("utf-8")),
            MAX_CONTEXT_BYTES + 1_000,
        )
        self.assertEqual(validate_conversation_context(context), context)
        self.assertIn("Turn 1 user:", render_conversation_context(context))
        contextualized = contextualize_question("Current question", context)
        self.assertIn("untrusted dialogue context only", contextualized)
        self.assertIn("Current user question:\nCurrent question", contextualized)

        tampered = {**context, "turn_count": context["turn_count"] + 1}
        with self.assertRaises(ValueError):
            validate_conversation_context(tampered)

    def test_empty_context_is_canonical(self):
        context = build_conversation_context([])
        self.assertEqual(context["turn_count"], 0)
        self.assertEqual(render_conversation_context(context), "(none)")
        self.assertEqual(contextualize_question("Current question", context), "Current question")


if __name__ == "__main__":
    unittest.main()
