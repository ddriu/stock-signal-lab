-- Per-owner checkpoint for an explicitly requested Radar review.
-- Market observations, orders, alert mail and daily reports are separate.
create table if not exists public.radar_review_checkpoints (
    owner text primary key check (owner ~ '^[A-Za-z0-9_@.+:\-]{1,128}$'),
    revision integer not null check (revision >= 1),
    checkpoint jsonb not null check (
        jsonb_typeof(checkpoint) = 'object'
        and not (checkpoint ?| array['owner','revision'])
        and octet_length(checkpoint::text) <= 262144
    )
);
alter table public.radar_review_checkpoints enable row level security;
revoke all on public.radar_review_checkpoints from public, anon, authenticated, service_role;
grant select, insert, update on public.radar_review_checkpoints to service_role;

create or replace function public.save_radar_review_checkpoint(
    p_owner text, p_expected_revision integer, p_checkpoint jsonb
) returns boolean
language plpgsql security invoker set search_path = pg_catalog
as $$
declare
    changed integer;
begin
    if p_expected_revision is null then
        insert into public.radar_review_checkpoints(owner, revision, checkpoint)
        values (p_owner, 1, p_checkpoint)
        on conflict (owner) do nothing;
    else
        if p_expected_revision < 1 or p_expected_revision >= 2147483647 then
            raise exception 'Invalid Radar checkpoint revision' using errcode = '22023';
        end if;
        update public.radar_review_checkpoints
        set revision = revision + 1, checkpoint = p_checkpoint
        where owner = p_owner and revision = p_expected_revision;
    end if;
    get diagnostics changed = row_count;
    return changed = 1;
end;
$$;
revoke all on function public.save_radar_review_checkpoint(text,integer,jsonb) from public, anon, authenticated;
grant execute on function public.save_radar_review_checkpoint(text,integer,jsonb) to service_role;
