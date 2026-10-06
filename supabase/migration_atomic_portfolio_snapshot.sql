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
