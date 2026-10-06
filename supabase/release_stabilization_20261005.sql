-- Estabilización 2026-10-05: ejecutar completo en Supabase > SQL Editor.
-- Sólo altera esquema/permisos; no reemplaza fotografías ni cambia filas.
-- Requiere las tablas de migration_portfolio_snapshots.sql y
-- migration_paper_simulation.sql. Un fallo revierte el bloque completo.
BEGIN;

DO $preflight$
DECLARE
    missing_columns text;
BEGIN
    IF EXISTS (
        SELECT 1 FROM (VALUES ('anon'), ('authenticated'), ('service_role')) AS expected(role_name)
        WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = expected.role_name)
    ) THEN
        RAISE EXCEPTION 'Faltan roles Supabase: anon, authenticated o service_role';
    END IF;

    IF to_regclass('public.portfolio_snapshots') IS NULL
       OR to_regclass('public.paper_daily_runs') IS NULL THEN
        RAISE EXCEPTION 'Faltan tablas base: instalar primero migration_portfolio_snapshots.sql y migration_paper_simulation.sql';
    END IF;

    SELECT string_agg(expected.column_name, ', ' ORDER BY expected.column_name)
    INTO missing_columns
    FROM (VALUES
        ('owner', 'text'), ('snapshot_date', 'date'), ('platform', 'text'),
        ('asset_name', 'text'), ('raw_identifier', 'text'), ('analysis_ticker', 'text'),
        ('asset_type', 'text'), ('portfolio_block', 'text'),
        ('quantity', 'double precision'), ('current_price', 'double precision'),
        ('currency', 'text'), ('value_eur', 'double precision'),
        ('return_pct', 'double precision'), ('cost_estimate_eur', 'double precision'),
        ('gain_loss_eur', 'double precision'), ('comments', 'text'),
        ('source', 'text'), ('notes', 'text'), ('recorded_by', 'text')
    ) AS expected(column_name, type_name)
    LEFT JOIN pg_attribute a
        ON a.attrelid = 'public.portfolio_snapshots'::regclass
        AND a.attname = expected.column_name AND a.attnum > 0 AND NOT a.attisdropped
    WHERE a.attname IS NULL OR a.atttypid <> expected.type_name::regtype;
    IF missing_columns IS NOT NULL THEN
        RAISE EXCEPTION 'portfolio_snapshots contiene columnas ausentes o incompatibles: %', missing_columns;
    END IF;

    SELECT string_agg(expected.column_name, ', ' ORDER BY expected.column_name)
    INTO missing_columns
    FROM (VALUES
        ('owner', 'text'), ('simulation_id', 'bigint'), ('market_date', 'date'),
        ('coverage_pct', 'double precision')
    ) AS expected(column_name, type_name)
    LEFT JOIN pg_attribute a
        ON a.attrelid = 'public.paper_daily_runs'::regclass
        AND a.attname = expected.column_name AND a.attnum > 0 AND NOT a.attisdropped
    WHERE a.attname IS NULL OR a.atttypid <> expected.type_name::regtype;
    IF missing_columns IS NOT NULL THEN
        RAISE EXCEPTION 'paper_daily_runs contiene columnas ausentes o incompatibles: %', missing_columns;
    END IF;
END;
$preflight$;

-- Ejecutar después de migration_portfolio_snapshots.sql. Una invocación RPC
-- valida e inserta toda la fotografía en la misma transacción que el borrado.
create or replace function public.replace_portfolio_snapshot(
    p_owner text,
    p_snapshot_date date,
    p_positions jsonb,
    p_recorded_by text default ''
) returns integer
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
    inserted_count integer;
begin
    if p_owner is null or btrim(p_owner) = '' or p_snapshot_date is null then
        raise exception 'La fotografía necesita propietario y fecha';
    end if;
    if p_positions is null or jsonb_typeof(p_positions) <> 'array' then
        raise exception 'La fotografía debe ser una lista de posiciones';
    end if;
    if jsonb_array_length(p_positions) = 0 then
        raise exception 'La fotografía no puede quedar vacía';
    end if;
    if exists (
        select 1 from jsonb_array_elements(p_positions) as item
        where jsonb_typeof(item) <> 'object'
           or coalesce(btrim(item ->> 'platform'), '') = ''
           or coalesce(btrim(item ->> 'asset_name'), '') = ''
    ) then
        raise exception 'Cada posición necesita plataforma y nombre';
    end if;

    -- Dos sustituciones del mismo usuario/fecha se ejecutan por orden.
    perform pg_advisory_xact_lock(
        hashtextextended('portfolio_snapshot:' || p_owner || ':' || p_snapshot_date::text, 0)
    );
    delete from public.portfolio_snapshots
    where owner = p_owner and snapshot_date = p_snapshot_date;

    insert into public.portfolio_snapshots (
        owner, snapshot_date, platform, asset_name, raw_identifier, analysis_ticker,
        asset_type, portfolio_block, quantity, current_price, currency, value_eur,
        return_pct, cost_estimate_eur, gain_loss_eur, comments, source, notes, recorded_by
    )
    select p_owner, p_snapshot_date, r.platform, r.asset_name,
        coalesce(r.raw_identifier, ''), coalesce(r.analysis_ticker, ''),
        coalesce(r.asset_type, ''), coalesce(r.portfolio_block, ''),
        r.quantity, r.current_price, coalesce(r.currency, 'EUR'), r.value_eur,
        r.return_pct, r.cost_estimate_eur, r.gain_loss_eur,
        coalesce(r.comments, ''), coalesce(r.source, ''), coalesce(r.notes, ''),
        coalesce(p_recorded_by, '')
    from jsonb_to_recordset(p_positions) as r (
        platform text, asset_name text, raw_identifier text, analysis_ticker text,
        asset_type text, portfolio_block text, quantity double precision,
        current_price double precision, currency text, value_eur double precision,
        return_pct double precision, cost_estimate_eur double precision,
        gain_loss_eur double precision, comments text, source text, notes text
    );
    get diagnostics inserted_count = row_count;
    return inserted_count;
end;
$$;

revoke all on function public.replace_portfolio_snapshot(text, date, jsonb, text)
    from public, anon, authenticated;
grant execute on function public.replace_portfolio_snapshot(text, date, jsonb, text)
    to service_role;

-- Las ejecuciones antiguas conservan NULL: su cobertura comparativa no se conoce.
alter table public.paper_daily_runs
    add column if not exists hold_coverage_pct double precision
        check (hold_coverage_pct between 0 and 100),
    add column if not exists benchmark_coverage_pct double precision
        check (benchmark_coverage_pct between 0 and 100);

DO $verify$
DECLARE
    function_oid oid;
BEGIN
    IF (
        SELECT count(*) FROM pg_attribute
        WHERE attrelid = 'public.paper_daily_runs'::regclass
          AND attname IN ('hold_coverage_pct', 'benchmark_coverage_pct')
          AND attnum > 0 AND NOT attisdropped
          AND atttypid = 'double precision'::regtype AND NOT attnotnull
    ) <> 2 THEN
        RAISE EXCEPTION 'Coberturas comparativas ausentes o incompatibles: deben ser double precision y permitir NULL';
    END IF;

    function_oid := to_regprocedure('public.replace_portfolio_snapshot(text,date,jsonb,text)');
    IF function_oid IS NULL THEN
        RAISE EXCEPTION 'No se ha creado la función atómica replace_portfolio_snapshot';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_proc WHERE oid = function_oid AND prosecdef)
       OR NOT EXISTS (
           SELECT 1 FROM pg_proc WHERE oid = function_oid
             AND proconfig @> ARRAY['search_path=public, pg_temp']
       ) THEN
        RAISE EXCEPTION 'La función debe ser SECURITY INVOKER y fijar su search_path';
    END IF;
    IF has_function_privilege('anon', function_oid, 'EXECUTE')
       OR has_function_privilege('authenticated', function_oid, 'EXECUTE')
       OR NOT has_function_privilege('service_role', function_oid, 'EXECUTE') THEN
        RAISE EXCEPTION 'Permisos inseguros o incompletos en replace_portfolio_snapshot';
    END IF;
END;
$verify$;

-- La actualización de la caché de PostgREST se entrega sólo al confirmar.
NOTIFY pgrst, 'reload schema';
COMMIT;
