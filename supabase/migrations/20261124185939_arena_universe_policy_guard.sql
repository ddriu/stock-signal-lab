-- Bind every new report and signal export to the current owner-approved policy.
-- No rows are seeded, no pause is lifted, and no pending plan is executed here.
create or replace function arena_private.validate_report_universe_policy(
  p_owner text, p_market_date date, p_report jsonb
) returns void language plpgsql security invoker set search_path='' as $$
declare
  policy_row public.arena_universe_policies%rowtype;
  audit jsonb;
  manifest jsonb;
  policy jsonb;
  item jsonb;
  codes jsonb;
  expected_codes jsonb;
  pending_tickers jsonb;
  selected_tickers jsonb;
  explicit_exclusions jsonb;
  owner_exclusions jsonb;
  expected_owner_exclusions jsonb;
  expected_global jsonb;
  canonical_policy text;
  expected_identity text;
  approved timestamptz;
  expected_reason text;
  pending_veto_count integer;
begin
  -- Also serialize the missing-row strict default with a concurrent approval.
  -- Consumers share this short lock; policy INSERT/UPDATE waits until validation
  -- and its protected report write/export commit.
  lock table public.arena_universe_policies in share mode;
  select * into policy_row from public.arena_universe_policies where owner=p_owner;
  audit := p_report->'arena_universe';
  if not found then
    if audit->>'mode'='verified_only' then
      raise exception using errcode='P0001',message='El informe no corresponde a la política actual de Arena.';
    end if;
    return;
  end if;
  canonical_policy := '{"approved_at":' ||
    case when policy_row.approved_at is null then 'null' else
      to_jsonb(to_char(policy_row.approved_at at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US')||'+00:00')::text end ||
    ',"mode":'||to_jsonb(policy_row.mode)::text||',"owner":'||to_jsonb(policy_row.owner)::text||
    ',"version":'||to_jsonb(policy_row.version)::text||'}';
  expected_identity := encode(sha256(convert_to(canonical_policy,'UTF8')),'hex');
  if policy_row.version is distinct from 'arena-universe-policy-v1'
     or policy_row.policy_id is distinct from expected_identity
     or policy_row.approved_at>statement_timestamp() then
    raise exception using errcode='P0001',message='La política guardada de Arena no es verificable.';
  end if;
  if policy_row.mode='strict' then
    if audit->>'mode'='verified_only' then
      raise exception using errcode='P0001',message='El informe no corresponde a la política actual de Arena.';
    end if;
    return;
  end if;

  manifest := p_report->'arena_quality';
  if jsonb_typeof(p_report) is distinct from 'object'
     or jsonb_typeof(audit) is distinct from 'object'
     or jsonb_typeof(manifest) is distinct from 'object'
     or jsonb_typeof(p_report->'rows') is distinct from 'array'
     or p_report->>'as_of' is distinct from p_market_date::text
     or audit->>'version' is distinct from 'arena-universe-audit-v1'
     or audit->>'mode' is distinct from 'verified_only'
     or audit->>'target_session' is distinct from p_market_date::text then
    raise exception using errcode='P0001',message='El informe requiere la auditoría vigente del universo de Arena.';
  end if;
  if not (audit ?& array['version','mode','policy','target_session','required_count',
      'initial_eligible_count','initial_valid_count','eligible_count','valid_count',
      'pending_count','pending','global_errors','strict_errors'])
     or (select count(*) from jsonb_object_keys(audit))<>13
     or jsonb_typeof(audit->'policy') is distinct from 'object'
     or jsonb_typeof(audit->'pending') is distinct from 'array'
     or jsonb_typeof(audit->'strict_errors') is distinct from 'array'
     or jsonb_typeof(audit->'global_errors') is distinct from 'array'
     or jsonb_typeof(manifest->'favorite_tickers') is distinct from 'array'
     or jsonb_typeof(manifest->'eligible_tickers') is distinct from 'array'
     or jsonb_typeof(manifest->'explicit_exclusions') is distinct from 'array'
     or jsonb_typeof(manifest->'excluded') is distinct from 'array'
     or jsonb_typeof(manifest->'errors') is distinct from 'array'
     or manifest->>'expected_session' is distinct from p_market_date::text then
    raise exception using errcode='P0001',message='La auditoría del universo de Arena tiene un formato no válido.';
  end if;
  policy := audit->'policy';
  if not (policy ?& array['owner','mode','approved_at','version','policy_id'])
     or (select count(*) from jsonb_object_keys(policy))<>5
     or policy->>'owner' is distinct from p_owner
     or policy->>'mode' is distinct from policy_row.mode
     or policy->>'version' is distinct from policy_row.version
     or policy->>'policy_id' is distinct from policy_row.policy_id
     or jsonb_typeof(policy->'approved_at') is distinct from 'string' then
    raise exception using errcode='P0001',message='El informe no corresponde a la aprobación actual de Arena.';
  end if;
  approved := (policy->>'approved_at')::timestamptz;
  if approved is distinct from policy_row.approved_at or approved>statement_timestamp() then
    raise exception using errcode='P0001',message='La fecha de aprobación del universo de Arena no coincide.';
  end if;
  if exists(select 1 from unnest(array['required_count','initial_eligible_count',
       'initial_valid_count','eligible_count','valid_count','pending_count']) fields(key)
       where jsonb_typeof(audit->fields.key) is distinct from 'number' or not coalesce(audit->>fields.key ~ '^[0-9]+$',false))
     or exists(select 1 from unnest(array['required_count','eligible_count','valid_count']) fields(key)
       where jsonb_typeof(manifest->fields.key) is distinct from 'number' or not coalesce(manifest->>fields.key ~ '^[0-9]+$',false)) then
    raise exception using errcode='P0001',message='La cobertura del universo de Arena no es verificable.';
  end if;
  if audit->'eligible_count' is distinct from manifest->'eligible_count'
     or audit->'valid_count' is distinct from manifest->'valid_count'
     or audit->'initial_valid_count' is distinct from manifest->'valid_count'
     or audit->'pending_count' is distinct from to_jsonb(jsonb_array_length(audit->'pending'))
     or manifest->'eligible_count' is distinct from to_jsonb(jsonb_array_length(manifest->'eligible_tickers'))
     or (audit->>'required_count')::numeric <> (manifest->>'required_count')::numeric+jsonb_array_length(audit->'pending')
     or (manifest->>'valid_count')::numeric>(manifest->>'eligible_count')::numeric
     or (audit->>'initial_valid_count')::numeric>(audit->>'initial_eligible_count')::numeric
     or (audit->>'initial_eligible_count')::numeric>(audit->>'required_count')::numeric
     or (audit->>'required_count')::numeric>jsonb_array_length(manifest->'favorite_tickers') then
    raise exception using errcode='P0001',message='Los recuentos del universo de Arena no coinciden.';
  end if;

  for item in select value from jsonb_array_elements(audit->'strict_errors') union all
              select value from jsonb_array_elements(audit->'global_errors') union all
              select value from jsonb_array_elements(manifest->'errors')
  loop
    if jsonb_typeof(item) is distinct from 'object' or (select count(*) from jsonb_object_keys(item))<>2
       or not (item ?& array['ticker','code']) or jsonb_typeof(item->'code') is distinct from 'string'
       or jsonb_typeof(item->'ticker') not in ('string','null') then
      raise exception using errcode='P0001',message='Los motivos del universo de Arena no son verificables.';
    end if;
  end loop;

  for item in select value from jsonb_array_elements(audit->'pending')
  loop
    if jsonb_typeof(item) is distinct from 'object' or (select count(*) from jsonb_object_keys(item))<>3
       or not (item ?& array['ticker','codes','reason']) or jsonb_typeof(item->'ticker') is distinct from 'string'
       or not coalesce(item->>'ticker' ~ '^[A-Z][A-Z0-9-]{0,11}$',false)
       or not coalesce(manifest->'favorite_tickers' ? (item->>'ticker'),false)
       or jsonb_typeof(item->'codes') is distinct from 'array'
       or jsonb_typeof(item->'reason') is distinct from 'string' then
      raise exception using errcode='P0001',message='Los valores pendientes de Arena no son verificables.';
    end if;
    codes := item->'codes';
    if jsonb_array_length(codes)=0 or exists(
      select 1 from jsonb_array_elements(codes) c where jsonb_typeof(c) is distinct from 'string'
        or (c#>>'{}') not in ('missing_analysis','calendar_unverified','analysis_session_mismatch',
          'price_missing','price_session_mismatch','price_currency_unverified','price_identity_unverified',
          'snapshot_identity_missing','observation_time_unverified','observation_before_close',
          'quality_unverified','growth_incomplete')) then
      raise exception using errcode='P0001',message='Los motivos pendientes de Arena no están autorizados.';
    end if;
    select jsonb_agg(code order by code), 'verified_only:'||string_agg(code,',' order by code)
      into expected_codes,expected_reason
    from (select distinct c#>>'{}' as code from jsonb_array_elements(codes) c) d;
    if length(expected_reason)>160 then expected_reason:='verified_only:multiple_verification_errors'; end if;
    if codes is distinct from expected_codes or item->>'reason' is distinct from expected_reason then
      raise exception using errcode='P0001',message='La explicación pendiente de Arena no coincide.';
    end if;
    select coalesce(jsonb_agg(code order by code),'[]'::jsonb) into expected_codes
    from (select distinct e->>'code' as code from jsonb_array_elements(audit->'strict_errors') e
      where e->>'ticker'=item->>'ticker') d;
    if codes is distinct from expected_codes or exists(
       select 1 from jsonb_array_elements(p_report->'rows') r
       where r->>'ticker'=item->>'ticker' and r->'price_identity_verified'='false'::jsonb
         and r->>'snapshot_id' ~ '^[a-f0-9]{64}$' and jsonb_typeof(r->'price')='number') then
      raise exception using errcode='P0001',message='Una contradicción de precio no puede ocultarse como pendiente de Arena.';
    end if;
  end loop;
  select coalesce(jsonb_agg(ticker order by ticker),'[]'::jsonb) into pending_tickers
    from (select distinct p->>'ticker' as ticker from jsonb_array_elements(audit->'pending') p) d;
  select coalesce(jsonb_agg(ticker order by ticker),'[]'::jsonb) into selected_tickers
    from (select distinct t#>>'{}' as ticker from jsonb_array_elements(manifest->'eligible_tickers') t
      where jsonb_typeof(t)='string') d;
  if jsonb_array_length(pending_tickers)<>jsonb_array_length(audit->'pending')
     or selected_tickers is distinct from manifest->'eligible_tickers'
     or exists(select 1 from jsonb_array_elements_text(selected_tickers) t
        where coalesce(pending_tickers ? t,false)) then
    raise exception using errcode='P0001',message='El universo seleccionado de Arena tiene duplicados o solapamientos.';
  end if;
  select coalesce(jsonb_agg(jsonb_build_object('ticker',p->>'ticker','reason',p->>'reason') order by p->>'ticker'),'[]'::jsonb)
    into explicit_exclusions from jsonb_array_elements(audit->'pending') p;
  select coalesce(jsonb_agg(jsonb_build_object('ticker',p->>'ticker','code','owner_excluded',
      'reason',p->>'reason') order by p->>'ticker'),'[]'::jsonb)
    into expected_owner_exclusions from jsonb_array_elements(audit->'pending') p;
  select coalesce(jsonb_agg(e order by e->>'ticker'),'[]'::jsonb)
    into owner_exclusions from jsonb_array_elements(manifest->'excluded') e
    where e->>'code'='owner_excluded';
  if explicit_exclusions is distinct from manifest->'explicit_exclusions'
     or owner_exclusions is distinct from expected_owner_exclusions then
    raise exception using errcode='P0001',message='Las exclusiones no coinciden con los valores pendientes de Arena.';
  end if;
  select count(*) into pending_veto_count from jsonb_array_elements(audit->'pending') p where exists(
    select 1 from jsonb_array_elements(p_report->'rows') r where r->>'ticker'=p->>'ticker'
      and r->'quality'->>'status'='blocked' and r->'quality'->'block_entry'='true'::jsonb);
  if (audit->>'initial_eligible_count')::numeric <>
       (manifest->>'eligible_count')::numeric+jsonb_array_length(audit->'pending')-pending_veto_count then
    raise exception using errcode='P0001',message='La cobertura inicial y final de Arena no coincide.';
  end if;
  select coalesce(jsonb_agg(value order by coalesce(value->>'ticker',''),value->>'code'),'[]'::jsonb)
    into expected_global from (
      select e as value from jsonb_array_elements(audit->'strict_errors') e
        where e->>'ticker' is null or not coalesce(pending_tickers ? (e->>'ticker'),false)
      union select e from jsonb_array_elements(manifest->'errors') e
    ) d;
  if expected_global is distinct from audit->'global_errors'
     or (jsonb_array_length(expected_global)>0 and manifest->'authorized'='true'::jsonb) then
    raise exception using errcode='P0001',message='El informe no conserva las incidencias globales de Arena.';
  end if;
exception when data_exception then
  raise exception using errcode='P0001',message='La auditoría de la política de Arena no se pudo verificar.';
end;
$$;
revoke all on function arena_private.validate_report_universe_policy(text,date,jsonb) from public,anon,authenticated;
grant execute on function arena_private.validate_report_universe_policy(text,date,jsonb) to service_role;

create or replace function arena_private.enforce_report_universe_policy()
returns trigger language plpgsql security invoker set search_path='' as $$
begin
  perform arena_private.validate_report_universe_policy(new.owner,new.market_date,new.report_json);
  return new;
end;
$$;
revoke all on function arena_private.enforce_report_universe_policy() from public,anon,authenticated;
grant execute on function arena_private.enforce_report_universe_policy() to service_role;
create trigger enforce_arena_universe_policy before insert on public.daily_score_reports
for each row execute function arena_private.enforce_report_universe_policy();

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
  universe_audit jsonb;
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
    p_state := p_state - 'data_quality_pause' - 'paused_plan_archive';
    if current_state ? 'data_quality_pause' then
      p_state := jsonb_set(p_state, '{data_quality_pause}', current_state->'data_quality_pause', true);
    end if;
    if current_state ? 'paused_plan_archive' then
      p_state := jsonb_set(p_state, '{paused_plan_archive}', current_state->'paused_plan_archive', true);
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
  perform arena_private.validate_report_universe_policy('ddriu',report_row.market_date,report_row.report_json);
  universe_audit := report_row.report_json->'arena_universe';
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
  -- Only the owner-approved pending set may be removed from an older core's
  -- numeric-score export. Unknown extras, duplicate exports and missing selected
  -- instruments remain errors; strict-mode behavior is unchanged.
  if universe_audit->>'mode'='verified_only' then
    if exists(select 1 from jsonb_array_elements(result->'signals') s
       where not (coalesce(manifest->'eligible_tickers' ? (s->>'ticker'),false)
         or exists(select 1 from jsonb_array_elements(universe_audit->'pending') p where p->>'ticker'=s->>'ticker')))
       or exists(select 1 from jsonb_array_elements(result->'signals') s group by s->>'ticker' having count(*)>1) then
      raise exception using errcode='P0001',message='La exportación contiene señales ajenas o duplicadas al universo de Arena.';
    end if;
    select jsonb_set(result,'{signals}',coalesce(jsonb_agg(s.value order by s.value->>'ticker'),'[]'::jsonb))
      into result from jsonb_array_elements(result->'signals') s
      where coalesce(manifest->'eligible_tickers' ? (s.value->>'ticker'),false);
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
  if universe_audit->>'mode'='verified_only' then
    result := result||jsonb_build_object('arena_universe',universe_audit);
  end if;
  return result||jsonb_build_object('arena_quality',manifest);
end;
$$;
revoke all on function public.arena_cloud_request(text,text,uuid,jsonb) from public,anon,authenticated;
grant execute on function public.arena_cloud_request(text,text,uuid,jsonb) to service_role;
