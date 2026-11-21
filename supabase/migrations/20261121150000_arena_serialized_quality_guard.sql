-- Serialize the safety check with the protected operation. No league data,
-- historical records, token rules, or table grants are changed here.
create or replace function arena_private.locked_state(p_token text)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  current_state jsonb;
begin
  -- Authenticate through the existing core before acquiring a privileged lock.
  -- Its returned state may precede a concurrent pause: re-read under the lock.
  perform arena_private.cloud_request(p_token, 'read', null, null);
  select state into strict current_state
  from arena_private.league where id for update;
  return current_state;
end;
$$;
revoke all on function arena_private.locked_state(text) from public,anon,authenticated;
grant execute on function arena_private.locked_state(text) to service_role;

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
  report_row record;
  manifest jsonb;
  result jsonb;
  actual_favorites jsonb;
  exported_tickers jsonb;
begin
  if p_action not in ('claim', 'save', 'signals') or p_action is null then
    return arena_private.cloud_request(p_token, p_action, p_lease, p_state);
  end if;
  -- This row lock remains held through the core operation and RPC commit.
  current_state := arena_private.locked_state(p_token);
  if current_state ? 'data_quality_pause'
     and (current_state->'data_quality_pause'->'paused') is distinct from 'false'::jsonb then
    raise exception using errcode='P0001', message='Arena pausada por revisión de calidad de datos.';
  end if;
  if p_action = 'save' then
    if jsonb_typeof(p_state) is distinct from 'object' then
      raise exception using errcode='22023', message='El estado de Arena debe ser un objeto.';
    end if;
    -- A worker may replace simulation state, never the owner's pause control.
    -- Preserve even an explicitly lifted pause and reject forged/new controls.
    p_state := p_state - 'data_quality_pause';
    if current_state ? 'data_quality_pause' then
      p_state := jsonb_set(p_state, '{data_quality_pause}', current_state->'data_quality_pause', true);
    end if;
  end if;
  if p_action <> 'signals' then
    return arena_private.cloud_request(p_token, p_action, p_lease, p_state);
  end if;

  select report_id,market_date,report_json into report_row
  from public.daily_score_reports where owner='ddriu' and created_at<=statement_timestamp()
  order by market_date desc,created_at desc limit 1;
  if not found then
    raise exception using errcode='P0001', message='Falta un informe con evidencia de datos verificable.';
  end if;
  manifest := report_row.report_json->'arena_quality';
  if jsonb_typeof(manifest) is distinct from 'object'
     or manifest->>'version' is distinct from 'arena-data-quality-v1'
     or manifest->'authorized' is distinct from 'true'::jsonb
     or manifest->>'status' is distinct from 'verified'
     or manifest->'price_ready' is distinct from 'true'::jsonb
     or manifest->>'expected_session' is distinct from report_row.market_date::text
     or report_row.report_json->>'as_of' is distinct from report_row.market_date::text
     or report_row.report_json->>'report_id' is distinct from report_row.report_id
     or not coalesce(manifest->>'manifest_id' ~ '^[a-f0-9]{64}$',false)
     or jsonb_typeof(manifest->'eligible_tickers') is distinct from 'array'
     or jsonb_typeof(manifest->'favorite_tickers') is distinct from 'array'
     or jsonb_typeof(manifest->'errors') is distinct from 'array'
     or jsonb_typeof(manifest->'evidence') is distinct from 'array'
     or jsonb_typeof(manifest->'session_close_at') is distinct from 'string'
     or jsonb_typeof(manifest->'criteria') is distinct from 'object' then
    raise exception using errcode='P0001', message='El informe no acredita la calidad y cobertura requeridas por Arena.';
  end if;
  if jsonb_array_length(manifest->'errors')<>0
     or jsonb_array_length(manifest->'eligible_tickers')=0
     or manifest->'eligible_count' is distinct from to_jsonb(jsonb_array_length(manifest->'eligible_tickers'))
     or manifest->'valid_count' is distinct from manifest->'eligible_count'
     or not (manifest->'criteria' ?& array['favorite_universe_known','expected_session_closed','unique_rows',
       'all_current_prices','all_current_growth','all_calendar_verified','all_eligible_quality_unblocked',
       'all_snapshot_evidence','has_eligible_instruments'])
     or exists(select 1 from jsonb_each(manifest->'criteria') c where c.value is distinct from 'true'::jsonb) then
    raise exception using errcode='P0001', message='La revisión tiene datos pendientes o cobertura no verificada.';
  end if;
  if exists (
    select 1 from jsonb_array_elements_text(manifest->'eligible_tickers') t(ticker)
    where not exists (
      select 1 from jsonb_array_elements(manifest->'evidence') e
      where e->>'ticker'=t.ticker and e->>'session'=report_row.market_date::text
        and e->>'price_currency'='USD' and e->>'price_source'='analysis_snapshot'
        and e->>'snapshot_id' ~ '^[a-f0-9]{64}$'
        and jsonb_typeof(e->'price')='number' and (e->>'price')::numeric>0
        and (e->>'recorded_at')::timestamptz >= (manifest->>'session_close_at')::timestamptz
        and (e->>'recorded_at')::timestamptz <= statement_timestamp()
        and exists (
          select 1 from jsonb_array_elements(report_row.report_json->'rows') r
          where r->>'ticker'=t.ticker and r->>'session'=report_row.market_date::text
            and r->>'snapshot_id'=e->>'snapshot_id' and r->'price'=e->'price'
            and r->>'price_currency'='USD' and r->'price_identity_verified'='true'::jsonb
        )
    )
  ) then
    raise exception using errcode='P0001', message='Falta evidencia de precio y observación para alguna señal.';
  end if;
  select coalesce(jsonb_agg(ticker order by ticker),'[]'::jsonb) into actual_favorites
  from (select distinct upper(trim(ticker)) as ticker from public.favorites where owner='ddriu') f;
  if actual_favorites is distinct from manifest->'favorite_tickers' then
    raise exception using errcode='P0001', message='Las favoritas han cambiado desde el informe verificado.';
  end if;
  result := arena_private.cloud_request(p_token,p_action,p_lease,p_state);
  if result->>'id' is distinct from report_row.report_id
     or result->>'day' is distinct from report_row.market_date::text then
    raise exception using errcode='P0001', message='El informe cambió durante la revisión de Arena.';
  end if;
  select coalesce(jsonb_agg(s.value->>'ticker' order by s.value->>'ticker'),'[]'::jsonb)
    into exported_tickers from jsonb_array_elements(result->'signals') s;
  if exported_tickers is distinct from manifest->'eligible_tickers' then
    raise exception using errcode='P0001', message='Las señales no coinciden con el universo verificado.';
  end if;
  select jsonb_set(result,'{signals}',coalesce(jsonb_agg(s.value||jsonb_build_object(
    'snapshot_id',(select r->>'snapshot_id' from jsonb_array_elements(report_row.report_json->'rows') r
      where r->>'ticker'=s.value->>'ticker' and r->>'session'=report_row.market_date::text limit 1)
  ) order by s.value->>'ticker'),'[]'::jsonb)) into result
  from jsonb_array_elements(result->'signals') s;
  return result||jsonb_build_object('arena_quality',manifest);
end;
$$;
revoke all on function public.arena_cloud_request(text,text,uuid,jsonb) from public,anon,authenticated;
grant execute on function public.arena_cloud_request(text,text,uuid,jsonb) to service_role;
