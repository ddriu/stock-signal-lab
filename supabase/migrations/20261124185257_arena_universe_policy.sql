-- Explicit private consent for Arena universe selection. No activation or seed
-- rows: absence means strict. This table cannot send mail or touch the league.
create table public.arena_universe_policies (
    owner text primary key check (
        length(owner) between 1 and 128
        and owner ~ '^[A-Za-z0-9_@.+:-]{1,128}$'
        and owner !~ '[[:space:]]'
        and lower(owner) not in ('anon','anonymous','public','group','grupo_compartido')
    ),
    mode text not null check (mode in ('strict','verified_only')),
    approved_at timestamptz,
    version text not null check (version='arena-universe-policy-v1'),
    policy_id text not null check (length(policy_id)=64 and policy_id ~ '^[0-9a-f]{64}$'),
    check (mode='strict' or approved_at is not null),
    check (approved_at is null or (isfinite(approved_at) and approved_at <= statement_timestamp()))
);

alter table public.arena_universe_policies enable row level security;
revoke all on public.arena_universe_policies from public,anon,authenticated,service_role;
grant select,insert,update on public.arena_universe_policies to service_role;
