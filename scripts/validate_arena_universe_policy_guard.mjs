// Synthetic, isolated Postgres QA. Never connects to a backend or sends mail.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const root = process.argv[2] || process.cwd();
const modulePath = process.env.PGLITE_MODULE;
const {PGlite} = modulePath ? await import(pathToFileURL(modulePath).href) : await import('@electric-sql/pglite');
const db = new PGlite();
// A Python regression rebuilds these checked-in examples from the real selector.
const fixtures = JSON.parse(await readFile(path.join(root,'tests/fixtures/arena_universe_policy_guard.json'),'utf8'));
const clone = value => structuredClone(value);
const migration = name => readFile(path.join(root,'supabase/migrations',name),'utf8');
await db.exec(`
  create role anon; create role authenticated; create role service_role bypassrls;
  create schema arena_private;
  grant usage on schema arena_private to service_role;
  create table arena_private.league(id boolean primary key,state jsonb,lease uuid);
  alter table arena_private.league enable row level security;
  create table public.daily_score_reports(owner text,report_id text,market_date date,
    report_json jsonb,created_at timestamptz,primary key(owner,report_id));
  create table public.favorites(owner text,ticker text);
  create table public.operations(id integer,value text);
  insert into public.operations values(1,'preserved');
  insert into public.favorites values('ddriu','GOOD'),('ddriu','PENDING'),('synthetic-other','OTHER');
  grant select,insert on public.daily_score_reports to service_role;
  grant select on public.favorites to service_role;
  insert into arena_private.league values(true,
    '{"rounds":[{"day":"2001-01-01"}],"pending":{"plans":{"synthetic":{}}},
       "competitors":[{"cash":100}],"data_quality_pause":{"paused":true,"reason_code":"synthetic"},
       "paused_plan_archive":[{"pending":{"day":"2001-01-02"},"reason":"synthetic-quarantine"}]}',null);
  create function arena_private.cloud_request(p_token text,p_action text,p_lease uuid,p_state jsonb)
  returns jsonb language plpgsql security definer set search_path='' as $$
  declare result jsonb; report record; signals jsonb; mode text;
  begin
    if p_token is distinct from 'synthetic-token' then
      raise insufficient_privilege using message='Acceso rechazado.';
    end if;
    case p_action
      when 'read' then select state into result from arena_private.league where id;
      when 'claim' then update arena_private.league set lease=p_lease where id returning state into result;
      when 'save' then update arena_private.league set state=p_state where id returning 'true'::jsonb into result;
      when 'release' then update arena_private.league set lease=null where id; result:='true'::jsonb;
      when 'signals' then
        select report_id,market_date,report_json into report from public.daily_score_reports
          where owner='ddriu' order by market_date desc,created_at desc limit 1;
        -- The unchanged core can export a numeric score whose growth inputs are
        -- incomplete. It is the wrapper's job to drop approved pending only.
        select coalesce(jsonb_agg(jsonb_build_object('ticker',r->>'ticker','score',r->'score') order by r->>'ticker'),'[]')
          into signals from jsonb_array_elements(report.report_json->'rows') r
          where r->>'calendar'='XNYS' and r->>'session'=report.market_date::text
            and r->>'ticker' ~ '^[A-Z][A-Z0-9-]{0,11}$' and r->>'ticker'<>'SPY'
            and r->'quality'->'block_entry'='false'::jsonb and jsonb_typeof(r->'score')='number'
            and r->>'status'<>'review_data' and exists(select 1 from public.favorites f
              where f.owner='ddriu' and f.ticker=r->>'ticker');
        mode:=current_setting('arena_test.export_mode',true);
        if mode='extra' then signals:=signals||'[{"ticker":"UNKNOWN","score":80}]'::jsonb;
        elsif mode='duplicate' then signals:=signals||jsonb_build_array(signals->0);
        elsif mode='missing' then signals:='[]';
        end if;
        result:=jsonb_build_object('id',report.report_id,'day',report.market_date,'signals',signals);
        if mode='wrong_id' then result:=jsonb_set(result,'{id}','"unexpected"'); end if;
    end case;
    return result;
  end $$;
  revoke all on function arena_private.cloud_request(text,text,uuid,jsonb) from public,anon,authenticated;
  grant execute on function arena_private.cloud_request(text,text,uuid,jsonb) to service_role;
`);
const before = async () => ({
  league:(await db.query('select * from arena_private.league')).rows,
  favorites:(await db.query('select * from public.favorites order by owner,ticker')).rows,
  operations:(await db.query('select * from public.operations')).rows,
  reports:(await db.query('select * from public.daily_score_reports order by owner,report_id')).rows,
});
await db.exec(await migration('20261121150000_arena_serialized_quality_guard.sql'));
await db.exec(await migration('20261124185257_arena_universe_policy.sql'));
const initial = await before();
if (!process.env.ARENA_POLICY_GUARD_SKIP) {
  await db.exec(await migration('20261124185939_arena_universe_policy_guard.sql'));
}
assert.deepEqual(await before(),initial,'DDL must not edit league, favorites, operations or reports');
let serial = 0;
const insertReport = async value => {
  const report=clone(value);
  // Distinct identities avoid a duplicate key masking an unexpectedly accepted
  // invalid payload. No assertion here depends on a report's self-claimed hash.
  report.report_id=createHash('sha256').update(JSON.stringify(report)+String(++serial)).digest('hex');
  await db.query(`insert into public.daily_score_reports values('ddriu',$1,'2026-11-23',$2,clock_timestamp())`,[report.report_id,report]);
  return report;
};
const setPolicy = async value => db.query(`insert into public.arena_universe_policies
  (owner,mode,approved_at,version,policy_id) values($1,$2,$3,$4,$5)
  on conflict(owner) do update set mode=excluded.mode,approved_at=excluded.approved_at,
    version=excluded.version,policy_id=excluded.policy_id`,
  [value.owner,value.mode,value.approved_at,value.version,value.policy_id]);
const signals = async () => (await db.query("select public.arena_cloud_request('synthetic-token','signals') as value")).rows[0].value;
const setMode = async value => db.query("select set_config('arena_test.export_mode',$1,false)",[value]);

await db.exec('set role service_role');
// Existing strict writers remain compatible before owner approval.
await insertReport(fixtures.strict);
await setPolicy(fixtures.policy);
// This is the red regression with ARENA_POLICY_GUARD_SKIP=1: old writers must
// not supersede the latest report with an unaudited strict payload.
await assert.rejects(() => insertReport(fixtures.strict),/auditoría vigente|aprobación|política/);
await insertReport(fixtures.verified);
await insertReport(fixtures.growth_pending);
await insertReport(fixtures.quality_pending);
await insertReport(fixtures.all_pending);
await insertReport(fixtures.conflict); // Blocked archives remain visible, never signal-authorized.
await assert.rejects(() => signals(),/Arena pausada/);
await db.exec('reset role');
await db.exec("update arena_private.league set state=jsonb_set(state,'{data_quality_pause,paused}','false')");
await db.exec('set role service_role');
await assert.rejects(() => signals(),/calidad|cobertura|pendientes/);

const malformed = [
  r => {delete r.arena_universe;},
  r => {r.arena_universe.version='future-v2';},
  r => {r.arena_universe.target_session='2026-11-20';},
  r => {r.arena_universe.policy.owner='synthetic-other';},
  r => {r.arena_universe.policy.policy_id='f'.repeat(64);},
  r => {r.arena_universe.policy.version='future-v2';},
  r => {r.arena_universe.policy.approved_at='2026-11-24T15:00:01Z';},
  r => {r.arena_universe.policy.approved_at='not-a-date';},
  r => {r.arena_universe.policy.approved_at='999999999999-11-24';},
  r => {r.arena_universe.pending_count=0;},
  r => {r.arena_universe.valid_count=true;},
  r => {r.arena_universe.required_count=99;},
  r => {r.arena_universe.initial_eligible_count=0;},
  r => {r.arena_universe.pending[0].ticker='UNKNOWN';},
  r => {r.arena_universe.pending[0].codes=['invented_code'];},
  r => {r.arena_universe.pending[0].codes.push('missing_analysis');},
  r => {r.arena_universe.pending[0].reason='hide performance';},
  r => {r.arena_universe.pending.push(clone(r.arena_universe.pending[0]));},
  r => {r.arena_universe.strict_errors=[];},
  r => {r.arena_universe.global_errors=[{ticker:null,code:'unknown_global'}];},
  r => {r.arena_quality.explicit_exclusions=[];},
  r => {r.arena_quality.excluded=[];},
  r => {r.arena_quality.excluded.push(clone(r.arena_quality.excluded[0]));},
  r => {r.arena_quality.excluded[0].unverified_field=true;},
  r => {r.arena_quality.eligible_tickers=['GOOD','GOOD'];},
  r => {r.arena_quality.eligible_tickers=['PENDING'];},
  r => {r.rows.push({...clone(r.rows[0]),ticker:'PENDING',price_identity_verified:false});},
];
for (const alter of malformed) {
  const invalid=clone(fixtures.verified); alter(invalid);
  await assert.rejects(() => insertReport(invalid),/universo|Arena|política|auditoría|exclusiones|contradicción|recuentos|cobertura|aprobación|motivos|pendientes/i);
}
// Semantically equivalent time zones are accepted (JSON formatting is not consent).
const equivalent=clone(fixtures.verified);
equivalent.arena_universe.policy.approved_at='2026-11-24T16:00:00+01:00';
await insertReport(equivalent);
let result=await signals();
assert.deepEqual(result.signals.map(s=>s.ticker),['GOOD']);
assert.equal(result.signals[0].snapshot_id,'a'.repeat(64));
assert.equal(result.arena_quality.authorized,true);
assert.equal(result.arena_universe.policy.policy_id,fixtures.policy.policy_id);

// The real core shape exports GOOD and a numeric-score, growth-incomplete
// PENDING. Only the exact approved pending set can be filtered from raw output.
await insertReport(fixtures.growth_pending);
result=await signals();
assert.deepEqual(result.signals.map(s=>s.ticker),['GOOD']);
assert.deepEqual(result.arena_universe.pending[0].codes,['growth_incomplete']);
for (const mode of ['extra','duplicate','missing','wrong_id']) {
  await setMode(mode);
  await assert.rejects(() => signals(),/exportación|universo|informe|señales/);
}
await setMode('');
await setPolicy(fixtures.changed_policy);
await assert.rejects(() => signals(),/aprobación actual/);
await setPolicy(fixtures.strict_policy);
await assert.rejects(() => signals(),/política actual/);
await setPolicy(fixtures.policy);

// Current favorite membership remains an exact prerequisite, including pending.
await db.exec('reset role');
await db.exec("insert into public.favorites values('ddriu','NEW')");
await db.exec('set role service_role');
await assert.rejects(() => signals(),/favoritas han cambiado/);
await db.exec('reset role');
await db.exec("delete from public.favorites where owner='ddriu' and ticker='NEW'");

const state=(await db.query('select state from arena_private.league')).rows[0].state;
await db.exec('set role service_role');
for (const incoming of [
  {rounds:state.rounds,competitors:state.competitors,pending:state.pending},
  {...state,paused_plan_archive:[],data_quality_pause:{paused:true,reason_code:'forged'}},
  {...state,paused_plan_archive:[{replacement:'forged'}]},
]) {
  assert.equal((await db.query("select public.arena_cloud_request('synthetic-token','save',null,$1) as ok",[incoming])).rows[0].ok,true);
  assert.deepEqual((await db.query("select public.arena_cloud_request('synthetic-token','read') as state")).rows[0].state,state);
}
await assert.rejects(() => db.query("select public.arena_cloud_request('wrong-token','signals')"),/Acceso rechazado/);
await assert.rejects(() => db.query('select * from arena_private.league'),/permission denied/);
await db.exec('begin');
await signals();
await db.exec('reset role');
assert.ok((await db.query(`select count(*)::int as n from pg_locks where
  relation='arena_private.league'::regclass and mode='RowShareLock' and granted`)).rows[0].n>0);
assert.ok((await db.query(`select count(*)::int as n from pg_locks where
  relation='public.arena_universe_policies'::regclass and mode='ShareLock' and granted`)).rows[0].n>0);
await db.exec('rollback; reset role');
for (const role of ['anon','authenticated']) {
  await db.exec(`set role ${role}`);
  await assert.rejects(() => db.query("select public.arena_cloud_request('synthetic-token','read')"),/permission denied/);
  await assert.rejects(() => db.query("select arena_private.validate_report_universe_policy('ddriu','2026-11-23','{}')"),/permission denied/);
  await assert.rejects(() => db.query('select * from public.arena_universe_policies'),/permission denied/);
  await db.exec('reset role');
}
// A stale worker cannot invent an archive when none exists.
await db.exec("update arena_private.league set state=state-'paused_plan_archive'");
await db.exec('set role service_role');
await db.query("select public.arena_cloud_request('synthetic-token','save',null,$1)",[state]);
assert.equal(Object.hasOwn((await db.query("select public.arena_cloud_request('synthetic-token','read') as state")).rows[0].state,'paused_plan_archive'),false);
await db.exec('reset role');
// All mutating operations still honor the durable pause, including save.
await db.exec("update arena_private.league set state=jsonb_set(state,'{data_quality_pause,paused}','true')");
const paused=(await db.query('select * from arena_private.league')).rows;
await db.exec('set role service_role');
for (const action of ['claim','save','signals']) {
  await assert.rejects(() => db.query("select public.arena_cloud_request('synthetic-token',$1,null,$2)",[action,state]),/Arena pausada/);
}
await db.exec('reset role');
assert.deepEqual((await db.query('select * from arena_private.league')).rows,paused);
assert.deepEqual((await db.query('select * from public.operations')).rows,initial.operations);
assert.deepEqual((await db.query('select * from public.favorites order by owner,ticker')).rows,initial.favorites);
await db.close();
console.log('PASS current-policy report/exports, exact pending filter, blocked archives, immutable quarantine, pause/private grants and lock retention (PGlite synthetic, not multiconnection concurrency).');
