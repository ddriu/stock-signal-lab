-- Transport cache only; never stores a signal, portfolio change or delivery.
create table if not exists public.market_data_cache (
    owner text not null check (owner ~ '^[A-Za-z0-9_@.+:\-]{1,128}$'),
    kind text not null check (kind in ('prices','fundamentals','fx')),
    symbol text not null check (symbol ~ '^[A-Za-z0-9_^.=:/\-]{1,64}$'),
    observed_at timestamptz not null,
    metadata jsonb not null check (jsonb_typeof(metadata) = 'object' and octet_length(metadata::text) <= 16384),
    payload jsonb not null check (jsonb_typeof(payload) = 'object' and octet_length(payload::text) <= 524288),
    primary key (owner, kind, symbol)
);
alter table public.market_data_cache enable row level security;
-- Existing application authentication is server-side. Browser roles must not
-- see an owner's watchlist, even though the individual quotes are public data.
revoke all on public.market_data_cache from public, anon, authenticated;
grant select, insert, update on public.market_data_cache to service_role;

create or replace function public.upsert_market_data_cache(
    p_owner text, p_kind text, p_symbol text, p_observed_at timestamptz,
    p_metadata jsonb, p_payload jsonb
) returns boolean
language plpgsql security invoker set search_path = pg_catalog, public
as $$
declare changed integer;
begin
    if p_observed_at > clock_timestamp() + interval '5 minutes' then
        raise exception 'Future cache observation';
    end if;
    insert into public.market_data_cache(owner,kind,symbol,observed_at,metadata,payload)
    values(p_owner,p_kind,p_symbol,p_observed_at,p_metadata,p_payload)
    on conflict(owner,kind,symbol) do update set
        observed_at=excluded.observed_at, metadata=excluded.metadata, payload=excluded.payload
    where public.market_data_cache.observed_at <= excluded.observed_at;
    get diagnostics changed = row_count;
    return changed = 1;
end;
$$;
revoke all on function public.upsert_market_data_cache(text,text,text,timestamptz,jsonb,jsonb) from public, anon, authenticated;
grant execute on function public.upsert_market_data_cache(text,text,text,timestamptz,jsonb,jsonb) to service_role;
