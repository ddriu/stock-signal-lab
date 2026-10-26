-- Restored verbatim from the CLI-created migration already applied and tested.
-- Additive, private history. No recipients, SMTP contents or credentials.
BEGIN;
CREATE TABLE IF NOT EXISTS public.daily_score_reports (
    owner text NOT NULL CHECK (length(owner) BETWEEN 1 AND 320),
    report_id text NOT NULL CHECK (report_id ~ '^[0-9a-f]{64}$'),
    market_date date NOT NULL,
    report_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner, report_id),
    UNIQUE (owner, report_id, market_date),
    CHECK (jsonb_typeof(report_json) = 'object'),
    CHECK (report_json->>'report_id' = report_id),
    CHECK (report_json->>'as_of' = market_date::text),
    CHECK (octet_length(report_json::text) <= 2500000)
);
CREATE TABLE IF NOT EXISTS public.daily_digest_deliveries (
    owner text NOT NULL,
    delivery_key text NOT NULL CHECK (delivery_key ~ '^[0-9a-f]{64}$'),
    market_date date NOT NULL,
    report_id text NOT NULL,
    status text NOT NULL DEFAULT 'reserved'
        CHECK (status IN ('reserved', 'sent', 'failed', 'uncertain')),
    error_type text NOT NULL DEFAULT '' CHECK (error_type ~ '^[A-Za-z0-9_]{0,80}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    PRIMARY KEY (owner, delivery_key),
    FOREIGN KEY (owner, report_id, market_date)
        REFERENCES public.daily_score_reports(owner, report_id, market_date)
);
CREATE INDEX IF NOT EXISTS daily_score_reports_owner_date_idx
    ON public.daily_score_reports(owner, market_date DESC);
ALTER TABLE public.daily_score_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.daily_digest_deliveries ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.daily_score_reports FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.daily_digest_deliveries FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT ON public.daily_score_reports TO service_role;
GRANT SELECT, INSERT, UPDATE ON public.daily_digest_deliveries TO service_role;
COMMENT ON TABLE public.daily_score_reports IS
    'Immutable per-owner versions opened from the daily email link. Service-only; no public API reads.';
COMMENT ON TABLE public.daily_digest_deliveries IS
    'Atomic pre-SMTP at-most-once claims. Uncertain or failed sends are never automatically recycled.';
COMMIT;
