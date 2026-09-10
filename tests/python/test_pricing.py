import unittest

from services.orchestration.app.pricing import (
    estimate_openai_embedding_cost,
    estimate_openai_text_generation_cost,
)


class OpenAICostEstimationTests(unittest.TestCase):
    def test_prices_a_dated_fast_model_revision(self):
        estimate = estimate_openai_text_generation_cost(
            model="gpt-5.4-mini-2026-03-17",
            input_tokens=329,
            cached_input_tokens=0,
            output_tokens=39,
            service_tier="default",
        )

        self.assertIsNotNone(estimate)
        self.assertEqual(estimate.priced_model, "gpt-5.4-mini")
        self.assertAlmostEqual(estimate.amount_usd, 0.00042225, places=10)

    def test_cached_input_uses_its_distinct_rate(self):
        estimate = estimate_openai_text_generation_cost(
            model="gpt-5.4-mini",
            input_tokens=1_000,
            cached_input_tokens=800,
            output_tokens=100,
            service_tier=None,
        )

        self.assertIsNotNone(estimate)
        self.assertAlmostEqual(estimate.amount_usd, 0.00066, places=10)

    def test_long_context_reasoning_model_uses_the_published_tier(self):
        estimate = estimate_openai_text_generation_cost(
            model="gpt-5.5-2026-08-13",
            input_tokens=272_000,
            cached_input_tokens=0,
            output_tokens=1_000,
            service_tier="default",
        )

        self.assertIsNotNone(estimate)
        self.assertEqual(estimate.pricing_profile.split(":")[-1], "gpt-5.5-272k-plus")
        self.assertAlmostEqual(estimate.amount_usd, 3.475, places=10)

    def test_unknown_models_and_nonstandard_tiers_are_not_guessed(self):
        common = {
            "input_tokens": 100,
            "cached_input_tokens": 0,
            "output_tokens": 20,
        }
        self.assertIsNone(
            estimate_openai_text_generation_cost(
                model="gpt-unknown", service_tier="default", **common
            )
        )
        self.assertIsNone(
            estimate_openai_text_generation_cost(
                model="gpt-5.4-mini-unrecognized-2026-03-17",
                service_tier="default",
                **common,
            )
        )
        self.assertIsNone(
            estimate_openai_text_generation_cost(
                model="gpt-5.4-mini", service_tier="priority", **common
            )
        )

    def test_prices_embedding_usage(self):
        estimate = estimate_openai_embedding_cost(
            model="text-embedding-3-small",
            input_tokens=10,
        )

        self.assertIsNotNone(estimate)
        self.assertAlmostEqual(estimate.amount_usd, 0.0000002, places=12)

    def test_rejects_impossible_usage(self):
        with self.assertRaises(ValueError):
            estimate_openai_text_generation_cost(
                model="gpt-5.4-mini",
                input_tokens=10,
                cached_input_tokens=11,
                output_tokens=1,
                service_tier="default",
            )


if __name__ == "__main__":
    unittest.main()
