-- The previous "semantic" option was a sentence-boundary token window, not an
-- embedding/model-based semantic segmenter. Preserve behavior under a truthful
-- name and prevent new preferences from advertising an unavailable technique.

ALTER TABLE user_preferences
    DROP CONSTRAINT IF EXISTS user_preferences_chunk_strategy_check;

UPDATE user_preferences
SET default_chunk_strategy = 'sentence', updated_at = NOW()
WHERE default_chunk_strategy = 'semantic';

ALTER TABLE user_preferences
    ADD CONSTRAINT user_preferences_chunk_strategy_check
        CHECK (default_chunk_strategy IN ('token', 'sentence', 'recursive'));
