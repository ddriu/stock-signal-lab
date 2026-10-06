-- Las ejecuciones antiguas conservan NULL: su cobertura comparativa no se conoce.
alter table public.paper_daily_runs
    add column if not exists hold_coverage_pct double precision
        check (hold_coverage_pct between 0 and 100),
    add column if not exists benchmark_coverage_pct double precision
        check (benchmark_coverage_pct between 0 and 100);
