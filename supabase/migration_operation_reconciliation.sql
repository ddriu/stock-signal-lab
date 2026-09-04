-- Estabilización de cartera: conserva la cuenta y la liquidación real en EUR.
-- Ejecutar una vez en Supabase > SQL Editor antes de publicar esta versión.

alter table public.operations
    add column if not exists account_name text not null default '',
    add column if not exists settlement_amount_eur double precision,
    add column if not exists fee_eur double precision,
    add column if not exists fx_rate_to_eur double precision;

alter table public.operations
    drop constraint if exists operations_settlement_amount_eur_check,
    drop constraint if exists operations_fee_eur_check,
    drop constraint if exists operations_fx_rate_to_eur_check;

alter table public.operations
    add constraint operations_settlement_amount_eur_check
        check (settlement_amount_eur is null or settlement_amount_eur > 0),
    add constraint operations_fee_eur_check
        check (fee_eur is null or fee_eur >= 0),
    add constraint operations_fx_rate_to_eur_check
        check (fx_rate_to_eur is null or fx_rate_to_eur > 0);

create index if not exists operations_owner_account_executed_idx
    on public.operations (owner, account_name, executed_at desc, id desc);

-- La misma revisión que genera el correo queda visible en la aplicación.
alter table public.email_alert_states
    add column if not exists company_name text not null default '',
    add column if not exists growth_score integer,
    add column if not exists fundamental_score integer,
    add column if not exists opportunity_score integer,
    add column if not exists opportunity_status text not null default '',
    add column if not exists data_note text not null default '';

alter table public.email_alert_states
    drop constraint if exists email_alert_states_growth_score_check,
    drop constraint if exists email_alert_states_fundamental_score_check,
    drop constraint if exists email_alert_states_opportunity_score_check;

alter table public.email_alert_states
    add constraint email_alert_states_growth_score_check
        check (growth_score is null or growth_score between 0 and 100),
    add constraint email_alert_states_fundamental_score_check
        check (fundamental_score is null or fundamental_score between 0 and 100),
    add constraint email_alert_states_opportunity_score_check
        check (opportunity_score is null or opportunity_score between 0 and 100);
