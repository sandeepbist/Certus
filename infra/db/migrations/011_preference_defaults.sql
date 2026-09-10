-- Preferences must describe behavior the runtime actually supports.

UPDATE user_preferences
SET default_model = 'auto', updated_at = NOW()
WHERE default_model IN ('gpt-4o', 'gpt-4o-mini') OR default_model IS NULL;

ALTER TABLE user_preferences
    ALTER COLUMN theme SET DEFAULT 'system',
    ALTER COLUMN default_model SET DEFAULT 'auto',
    ALTER COLUMN default_chunk_strategy SET DEFAULT 'token',
    ALTER COLUMN theme SET NOT NULL,
    ALTER COLUMN default_model SET NOT NULL,
    ALTER COLUMN default_chunk_strategy SET NOT NULL,
    ALTER COLUMN notification_email SET NOT NULL,
    ALTER COLUMN notification_in_app SET NOT NULL,
    ALTER COLUMN notification_digest SET NOT NULL;

ALTER TABLE user_preferences
    DROP CONSTRAINT IF EXISTS user_preferences_theme_check,
    ADD CONSTRAINT user_preferences_theme_check
        CHECK (theme IN ('dark', 'light', 'system')),
    DROP CONSTRAINT IF EXISTS user_preferences_chunk_strategy_check,
    ADD CONSTRAINT user_preferences_chunk_strategy_check
        CHECK (default_chunk_strategy IN ('token', 'semantic', 'recursive'));
