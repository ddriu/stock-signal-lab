import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const root = process.argv[2] || process.cwd();
const modulePath = process.env.PGLITE_MODULE;
const { PGlite } = modulePath
  ? await import(pathToFileURL(modulePath).href)
  : await import('@electric-sql/pglite');
const db = new PGlite();
await db.exec(`
  create role anon; create role authenticated; create role service_role;
  create schema arena_private;
  grant usage on schema arena_private to service_role;
  create table arena_private.league (id boolean primary key, token_hash bytea,
    state jsonb, lease uuid, expires_at timestamptz);
  alter table arena_private.league enable row level security;
  create function arena_private.cloud_request(p_token text,p_action text,p_lease uuid,p_state jsonb)
  returns jsonb language plpgsql security definer set search_path='' as $$
  declare result jsonb;
  begin
    if p_token is distinct from 'synthetic-token' then
      raise insufficient_privilege using message='Acceso rechazado.';
    end if;
    case p_action
      when 'read' then
        select state into result from arena_private.league where id;
        -- Deterministic interleaving: authentication returns the old state,
        -- while an owner pause is present before the wrapper's locked read.
        if current_setting('arena_test.pause_after_read',true)='on' then
          update arena_private.league set state=jsonb_set(state,'{data_quality_pause}',
            '{"paused":true,"reason_code":"synthetic_injected_pause"}') where id;
        end if;
      when 'claim' then update arena_private.league set lease=p_lease where id returning state into result;
      when 'save' then update arena_private.league set state=p_state where id returning 'true'::jsonb into result;
      when 'signals' then
        select jsonb_build_object('id',report_id,'day',market_date,
          'signals',jsonb_build_array(jsonb_build_object('ticker','SYNTH','score',80)))
        into result from public.daily_score_reports order by created_at desc limit 1;
      when 'release' then update arena_private.league set lease=null where id; result:='true'::jsonb;
    end case;
    return result;
  end $$;
  revoke all on function arena_private.cloud_request(text,text,uuid,jsonb) from public,anon,authenticated;
  grant execute on function arena_private.cloud_request(text,text,uuid,jsonb) to service_role;
  insert into arena_private.league values(true,null,
    '{"rounds":[{"date":"2026-01-02"}],"pending":{"plans":{"synthetic":{}}},"competitors":[{"cash":100}]}',null,null);
`);
const before = (await db.query(`select state from arena_private.league`)).rows[0].state;
const migration = await readFile(path.join(root, 'supabase/migrations/20261121140000_arena_data_quality_pause.sql'), 'utf8');
await db.exec(migration);
await db.exec(`set role service_role`);
const read = await db.query(`select public.arena_cloud_request('synthetic-token','read') as state`);
assert.equal(read.rows[0].state.data_quality_pause.paused, true);
const after = { ...read.rows[0].state };
delete after.data_quality_pause;
assert.deepEqual(after, before);
for (const action of ['claim', 'save', 'signals']) {
  await assert.rejects(db.query(`select public.arena_cloud_request('synthetic-token',$1,null,'{}')`, [action]), /Arena pausada/);
}
await assert.rejects(db.query(`select public.arena_cloud_request('wrong-token','read')`), /Acceso rechazado/);
assert.equal((await db.query(`select public.arena_cloud_request('synthetic-token','release') as ok`)).rows[0].ok, true);
for (const role of ['anon', 'authenticated']) {
  await db.exec(`reset role; set role ${role}`);
  await assert.rejects(db.query(`select public.arena_cloud_request('synthetic-token','read')`), /permission denied/);
}
await db.exec(`reset role`);
const preserved = (await db.query(`select state-'data_quality_pause' as state from arena_private.league`)).rows[0].state;
assert.deepEqual(preserved, before);
// Reapplication keeps the existing pause timestamp and every historical field.
const stamp = (await db.query(`select state->'data_quality_pause' as control from arena_private.league`)).rows[0].control;
await db.exec(migration);
assert.deepEqual((await db.query(`select state->'data_quality_pause' as control from arena_private.league`)).rows[0].control, stamp);
await db.exec(`
  create table public.daily_score_reports(owner text,report_id text,market_date date,report_json jsonb,created_at timestamptz);
  create table public.favorites(owner text,ticker text);
  grant select on public.daily_score_reports,public.favorites to service_role;
  insert into public.favorites values('ddriu','SYNTH');
`);
await db.exec(await readFile(path.join(root, 'supabase/migrations/20261121143000_arena_quality_manifest_gate.sql'), 'utf8'));
await db.exec(`set role service_role`);
await assert.rejects(db.query(`select public.arena_cloud_request('synthetic-token','signals')`), /Arena pausada/);
await db.exec(`reset role`);
const leagueBeforeGuard = (await db.query(`select * from arena_private.league`)).rows[0];
const serializedMigration = await readFile(path.join(root,'supabase/migrations/20261121150000_arena_serialized_quality_guard.sql'),'utf8');
await db.exec(serializedMigration);
assert.deepEqual((await db.query(`select * from arena_private.league`)).rows[0],leagueBeforeGuard);
await db.exec(`update arena_private.league set state=jsonb_set(state,'{data_quality_pause,paused}','false')`);
const criteria = Object.fromEntries(['favorite_universe_known','expected_session_closed','unique_rows',
  'all_current_prices','all_current_growth','all_calendar_verified','all_eligible_quality_unblocked',
  'all_snapshot_evidence','has_eligible_instruments'].map(key=>[key,true]));
const manifest = {version:'arena-data-quality-v1',authorized:true,status:'verified',price_ready:true,
  expected_session:'2026-01-02',eligible_count:1,valid_count:1,eligible_tickers:['SYNTH'],
  session_close_at:'2026-01-02T21:00:00Z',favorite_tickers:['SYNTH'],errors:[],
  evidence:[{ticker:'SYNTH',session:'2026-01-02',snapshot_id:'c'.repeat(64),price:100,
    price_currency:'USD',price_source:'analysis_snapshot',recorded_at:'2026-01-02T21:01:00Z'}],criteria,manifest_id:'a'.repeat(64)};
const report = {report_id:'b'.repeat(64),as_of:'2026-01-02',arena_quality:manifest,
  rows:[{ticker:'SYNTH',session:'2026-01-02',snapshot_id:'c'.repeat(64),price:100,
    price_currency:'USD',price_identity_verified:true}]};
await db.query(`insert into public.daily_score_reports values('ddriu',$1,'2026-01-02',$2,now())`,[report.report_id,report]);
await db.exec(`set role service_role`);
const exported = (await db.query(`select public.arena_cloud_request('synthetic-token','signals') as report`)).rows[0].report;
assert.equal(exported.arena_quality.authorized,true);
assert.equal(exported.signals[0].snapshot_id,'c'.repeat(64));
await db.exec(`reset role`);

// A stale worker must neither omit nor replace the authoritative pause control,
// including a deliberately lifted pause. Every other historical field survives.
const liftedControl = (await db.query(`select state->'data_quality_pause' as control from arena_private.league`)).rows[0].control;
await db.exec(`set role service_role`);
assert.equal((await db.query(`select public.arena_cloud_request('synthetic-token','save',null,$1) as ok`,[before])).rows[0].ok,true);
let savedState = (await db.query(`select public.arena_cloud_request('synthetic-token','read') as state`)).rows[0].state;
assert.deepEqual(savedState.data_quality_pause,liftedControl);
assert.deepEqual(Object.fromEntries(Object.entries(savedState).filter(([key])=>key!=='data_quality_pause')),before);
const forgedState = {...before,data_quality_pause:{paused:true,reason_code:'forged',paused_at:'1900-01-01'}};
assert.equal((await db.query(`select public.arena_cloud_request('synthetic-token','save',null,$1) as ok`,[forgedState])).rows[0].ok,true);
savedState = (await db.query(`select public.arena_cloud_request('synthetic-token','read') as state`)).rows[0].state;
assert.deepEqual(savedState.data_quality_pause,liftedControl);
for (const invalidState of [null,[],42,'invalid']) {
  await assert.rejects(db.query(`select public.arena_cloud_request('synthetic-token','save',null,$1::jsonb)`,
    [invalidState===null?null:JSON.stringify(invalidState)]),error=>error.code==='22023' && /debe ser un objeto/.test(error.message));
  assert.deepEqual((await db.query(`select public.arena_cloud_request('synthetic-token','read') as state`)).rows[0].state,savedState);
}

// The lock is acquired by the private helper without exposing the private table
// to the service role, and persists until the transaction ends.
await assert.rejects(db.query(`select * from arena_private.league`), /permission denied/);
await assert.rejects(db.query(`select arena_private.locked_state('wrong-token')`), /Acceso rechazado/);
await db.exec(`begin`);
assert.deepEqual((await db.query(`select arena_private.locked_state('synthetic-token') as state`)).rows[0].state,savedState);
await db.exec(`reset role`);
const rowShare = await db.query(`select count(*)::integer as count from pg_locks
  where relation='arena_private.league'::regclass and mode='RowShareLock' and granted`);
assert.ok(rowShare.rows[0].count>0,'SELECT FOR UPDATE must retain its lock until commit');
await db.exec(`rollback; reset role`);
assert.equal((await db.query(`select has_table_privilege('service_role','arena_private.league','SELECT') as allowed`)).rows[0].allowed,false);
for (const role of ['anon','authenticated']) {
  assert.equal((await db.query(`select has_function_privilege($1,'arena_private.locked_state(text)','EXECUTE') as allowed`,[role])).rows[0].allowed,false);
  await db.exec(`set role ${role}`);
  await assert.rejects(db.query(`select public.arena_cloud_request('synthetic-token','read')`), /permission denied/);
  await assert.rejects(db.query(`select arena_private.locked_state('synthetic-token')`), /permission denied/);
  await db.exec(`reset role`);
}

// The synthetic hook is not a multiconnection concurrency test. It proves the
// wrapper checks the state read after authentication under the acquired lock.
for (const action of ['claim','save','signals']) {
  await db.exec(`set role service_role; select set_config('arena_test.pause_after_read','on',false)`);
  await assert.rejects(db.query(`select public.arena_cloud_request('synthetic-token',$1,null,$2)`,[action,before]), /Arena pausada/);
  await db.exec(`select set_config('arena_test.pause_after_read','off',false); reset role`);
  assert.deepEqual((await db.query(`select state from arena_private.league`)).rows[0].state,savedState);
}

// With no owner control at all, an incoming worker cannot create one either.
await db.exec(`update arena_private.league set state=state-'data_quality_pause'`);
await db.exec(`set role service_role`);
assert.equal((await db.query(`select public.arena_cloud_request('synthetic-token','save',null,$1) as ok`,[forgedState])).rows[0].ok,true);
assert.deepEqual((await db.query(`select public.arena_cloud_request('synthetic-token','read') as state`)).rows[0].state,before);
await db.exec(`reset role`);

// Reapplying the serialized guard must not edit state, leases, or permissions.
const leagueBeforeReapply = (await db.query(`select * from arena_private.league`)).rows[0];
await db.exec(serializedMigration);
assert.deepEqual((await db.query(`select * from arena_private.league`)).rows[0],leagueBeforeReapply);
await db.query(`update arena_private.league set state=jsonb_set(state,'{data_quality_pause}',$1)`,[stamp]);
await db.exec(`set role service_role`);
for (const action of ['claim','save','signals']) {
  await assert.rejects(db.query(`select public.arena_cloud_request('synthetic-token',$1,null,$2)`,[action,before]), /Arena pausada/);
}
assert.equal((await db.query(`select public.arena_cloud_request('synthetic-token','release') as ok`)).rows[0].ok,true);
await db.exec(`reset role`);
assert.deepEqual((await db.query(`select state-'data_quality_pause' as state from arena_private.league`)).rows[0].state,before);
await db.query(`update arena_private.league set state=jsonb_set(state,'{data_quality_pause}',$1)`,[liftedControl]);

for (const broken of [null,{...manifest,authorized:false},{...manifest,criteria:{}},
  {...manifest,valid_count:0},{...manifest,expected_session:'2026-01-01'},
  {...manifest,favorite_tickers:['OTHER']},{...manifest,eligible_tickers:['OTHER']},
  {...manifest,evidence:[]},{...manifest,evidence:[{...manifest.evidence[0],price_currency:'EUR'}]}]) {
  const invalid = {...report,arena_quality:broken};
  await db.query(`update public.daily_score_reports set report_json=$1`,[invalid]);
  await db.exec(`set role service_role`);
  await assert.rejects(db.query(`select public.arena_cloud_request('synthetic-token','signals')`),
    /informe|revisión|favoritas|señales|evidencia/i);
  await db.exec(`reset role`);
}
await db.query(`update public.daily_score_reports set report_json=$1`,[report]);
await db.exec(`insert into public.favorites values('ddriu','NEW')`);
await db.exec(`set role service_role`);
await assert.rejects(db.query(`select public.arena_cloud_request('synthetic-token','signals')`), /favoritas han cambiado/);
await db.close();
console.log('Arena pause/manifest: authentication, denied writes, preserved history/control, private grants, exact coverage, locked re-read and idempotency PASS (PGlite synthetic; no multiconnection concurrency test).');
