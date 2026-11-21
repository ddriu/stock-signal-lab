-- A non-empty subset is not proof of coverage. Require the sealed report's
-- explicit US-favorite evidence before exporting signals; retain the pause.
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
  current_state := arena_private.cloud_request(p_token, 'read', null, null);
  if p_action in ('claim', 'save', 'signals')
     and current_state ? 'data_quality_pause'
     and (current_state->'data_quality_pause'->'paused') is distinct from 'false'::jsonb then
    raise exception using errcode='P0001', message='Arena pausada por revisión de calidad de datos.';
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
