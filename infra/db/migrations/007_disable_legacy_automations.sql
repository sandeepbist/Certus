-- Prevent legacy prototype definitions from executing with broader semantics than intended.
-- Users can review and recreate these rules through the validated automation editor.

UPDATE automation_rules
SET is_enabled = false,
    updated_at = NOW(),
    version = version + 1
WHERE is_enabled = true
  AND (
      trigger_event <> 'on_document_uploaded'
      OR NOT (trigger_conditions = '{}'::jsonb OR trigger_conditions ? 'type')
      OR EXISTS (
          SELECT 1
          FROM jsonb_array_elements(actions) AS action
          WHERE action->>'type' NOT IN ('summarize_and_create_task', 'notify')
      )
  );
