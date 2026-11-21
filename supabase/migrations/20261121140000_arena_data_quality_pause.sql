-- Owner-authorized safety pause. Preserve league history and pending plans.
-- Reads and lease release remain available; planning and saves are stopped.
create or replace function public.arena_cloud_request(
  p_token text, p_action text, p_lease uuid default null, p_state jsonb default null
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $$
declare
  current_state jsonb;
begin
  -- The existing private function authenticates the token before any status
  -- is disclosed. Do not bypass its authorization or duplicate its secrets.
  current_state := arena_private.cloud_request(p_token, 'read', null, null);
  if p_action in ('claim', 'save', 'signals')
     and current_state ? 'data_quality_pause'
     and (current_state->'data_quality_pause'->'paused') is distinct from 'false'::jsonb then
    raise exception using
      errcode = 'P0001', message = 'Arena pausada por revisión de calidad de datos.';
  end if;
  return arena_private.cloud_request(p_token, p_action, p_lease, p_state);
end;
$$;
revoke all on function public.arena_cloud_request(text,text,uuid,jsonb) from public, anon, authenticated;
grant execute on function public.arena_cloud_request(text,text,uuid,jsonb) to service_role;

update arena_private.league
set state = jsonb_set(state, '{data_quality_pause}', jsonb_build_object(
  'paused', true,
  'reason_code', 'market_data_validation',
  'paused_at', statement_timestamp()
), true)
where id and not (state->'data_quality_pause'->'paused' = 'true'::jsonb is true);
