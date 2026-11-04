-- Applied migration identity follows the hosted migration history.
-- The comparative evaluator is optional. NULL is unavailable, never zero.
-- Metadata-only change: preserve every historical score, ID and timestamp.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

DO $migration$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'analysis_snapshots'
      AND column_name = 'opportunity_score' AND data_type = 'integer'
  ) THEN
    RAISE EXCEPTION 'Expected integer comparative score column is missing';
  END IF;
END
$migration$;

ALTER TABLE public.analysis_snapshots
  ALTER COLUMN opportunity_score DROP NOT NULL;

COMMENT ON COLUMN public.analysis_snapshots.opportunity_score IS
  'Optional comparative evaluator score (0-100). NULL means unavailable; independent primary strategy output remains in the versioned policy note.';
