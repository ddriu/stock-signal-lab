// Offline PostgreSQL safety checks. No credentials, URLs or remote DBs are used.
// PGLITE_MODULE may point to a temporary install's dist/index.js.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync, readdirSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const {PGlite} = await import(process.env.PGLITE_MODULE || '@electric-sql/pglite');
const repo = process.argv[2] || path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const migrationName = readdirSync(`${repo}/supabase/migrations`).find(name => name.endsWith('_model_results_ledger.sql'));
assert(migrationName, 'CLI-generated model-results migration not found.');
const migration = readFileSync(`${repo}/supabase/migrations/${migrationName}`, 'utf8');
const db = new PGlite();
const ns = 'model-original-v1', genesis = '0'.repeat(64);
let passed = 0;
const canonical = value => value === null || typeof value !== 'object' ? JSON.stringify(value)
  : Array.isArray(value) ? `[${value.map(canonical).join(',')}]`
    : `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
const sha = raw => createHash('sha256').update(raw, 'utf8').digest('hex');
const clone = value => JSON.parse(JSON.stringify(value));
async function query(sql, args=[]) { return (await db.query(sql, args)).rows; }
async function rpc(name, request) { return (await query(`SELECT public.${name}($1::jsonb) AS value`, [JSON.stringify(request)]))[0].value; }
async function select(kind, season_id=null, owner='alice', run_id=null) {
  return rpc('model_results_select', {owner,namespace:ns,kind,season_id,run_id});
}
async function check(name, fn) {
  try { await fn(); }
  catch (error) { throw new Error(`${name}: ${error.message} [${error.code || 'assertion'}]`, {cause:error}); }
  passed++; process.stdout.write(`PASS ${name}\n`);
}
function seasonRequest(id='season-a', overrides={}) {
  const manifest = {season_key:'daily-2026',strategy_key:'A',mode:'budget',start_date:'2026-10-20',
    base_currency:'USD',initial_nav_usd:10000,git_commit:'c'.repeat(40),...overrides};
  const raw=canonical(manifest);
  return {owner:'alice',namespace:ns,season_id:id,manifest,manifest_hash:sha(raw),manifest_canonical:raw,paper_header:null};
}
function event(id='decision-1', overrides={}) {
  const core={event_id:id,event_type:'decision',occurred_at:'2026-10-20T20:00:00.000000+00:00',
    available_at:'2026-10-20T20:00:00.000000+00:00',payload:{action:'hold',ticker:'AAPL'},...overrides};
  const raw=canonical(core);
  return {...core,event_hash:sha(raw),event_canonical:raw};
}
function batch(season_id='season-a', overrides={}) {
  const payload={available_at:'2026-10-20T20:00:00.000000+00:00',base_currency:'USD',cash_usd:10000,
    net_nav_usd:10000,benchmark_nav_usd:null,positions:[],counts:{candidates:0},quality_flags:['benchmark_missing']};
  const snapshot={payload,available_at:payload.available_at,snapshot_hash:sha(canonical(payload)),snapshot_canonical:canonical(payload)};
  const content={owner:'alice',namespace:ns,season_id,run_id:'daily-1',market_date:'2026-10-20',events:[event()],
    snapshot,paper_snapshot:null,paper_snapshot_canonical:null,...overrides};
  const expected=content.expected_previous_hash ?? genesis; delete content.expected_previous_hash;
  const suppliedId=content.batch_id; delete content.batch_id;
  const raw=canonical(content), digest=sha(raw);
  return {...content,content_hash:digest,content_canonical:raw,batch_id:suppliedId || digest,expected_previous_hash:expected};
}
function revisedSnapshot(nav=10001, at='2026-10-20T20:00:00.000000+00:00') {
  const payload={available_at:at,base_currency:'USD',net_nav_usd:nav,benchmark_nav_usd:null,positions:[]};
  return {payload,available_at:at,snapshot_hash:sha(canonical(payload)),snapshot_canonical:canonical(payload)};
}
async function counts() {
  return Promise.all(['seasons','batches','events','snapshots'].map(async kind => [kind,(await select(kind)).length]));
}
async function rejectedUnchanged(request, pattern) {
  const before=await counts();
  await assert.rejects(() => rpc('model_results_append_batch',request), pattern);
  assert.deepEqual(await counts(),before);
}

try {
  await db.waitReady;
  await db.exec('CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;');
  await db.exec(readFileSync(`${repo}/supabase/migration_paper_simulation.sql`,'utf8'));
  await db.exec(readFileSync(`${repo}/supabase/migration_paper_comparator_coverage.sql`,'utf8'));
  const legacy=(await query(`INSERT INTO public.paper_simulations(owner,name,strategy_key,engine_version,start_date,initial_nav_eur)
    VALUES('alice','Legacy lab','lab-v1','legacy','2026-01-01',1000) RETURNING id`))[0].id;
  await query(`INSERT INTO public.paper_daily_runs(owner,simulation_id,market_date,input_hash,cash_eur,net_nav_eur,hold_nav_eur,benchmark_nav_eur,engine_version)
    VALUES('alice',$1,'2026-10-01','legacy',0,1000,1000,1000,'legacy')`,[legacy]);
  const oldRuns=await query('SELECT * FROM public.paper_daily_runs');
  const oldHeaders=await query('SELECT * FROM public.paper_simulations');
  await check('Migration applies twice without changing laboratory history',async()=>{
    await db.exec(migration); await db.exec(migration);
    assert.deepEqual(await query('SELECT * FROM public.paper_daily_runs'),oldRuns);
    assert.deepEqual(await query('SELECT * FROM public.paper_simulations'),oldHeaders);
  });
  await check('Missing base tables fail before installing any ledger object',async()=>{
    const broken=new PGlite();
    try {
      await broken.waitReady;
      await broken.exec('CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;');
      await assert.rejects(()=>broken.exec(migration),/paper schema/);
      await broken.exec('ROLLBACK');
      assert.equal((await broken.query(`SELECT to_regclass('public.model_result_seasons') IS NULL AS absent`)).rows[0].absent,true);
    } finally { await broken.close(); }
  });
  await check('RLS enabled, invoker search_path fixed, RPC and tables service-only',async()=>{
    for(const name of ['model_results_create_season','model_results_append_batch','model_results_select']) {
      const sig=`public.${name}(jsonb)`;
      for(const role of ['anon','authenticated','service_role']) assert.equal((await query(`SELECT has_function_privilege($1,$2,'EXECUTE') AS allowed`,[role,sig]))[0].allowed,role==='service_role');
      const row=(await query('SELECT prosecdef,proconfig FROM pg_proc WHERE oid=$1::regprocedure',[sig]))[0];
      assert.equal(row.prosecdef,false); assert(row.proconfig.includes('search_path=public, pg_temp'));
    }
    for(const table of ['model_result_seasons','model_result_batches','model_result_events','model_result_snapshots']) {
      assert.equal((await query('SELECT relrowsecurity FROM pg_class WHERE oid=$1::regclass',[`public.${table}`]))[0].relrowsecurity,true);
      for(const role of ['anon','authenticated']) assert.equal((await query(`SELECT has_table_privilege($1,$2,'SELECT') AS allowed`,[role,`public.${table}`]))[0].allowed,false);
      assert.equal((await query(`SELECT has_table_privilege('service_role',$1,'UPDATE') AS allowed`,[`public.${table}`]))[0].allowed,false);
    }
  });
  await db.exec('SET ROLE service_role');
  let header, first;
  await check('USD header and native NULL snapshot do not fabricate EUR',async()=>{
    header=await rpc('model_results_create_season',seasonRequest());
    first=await rpc('model_results_append_batch',batch());
    assert.equal(header.paper_simulation_id,null); assert.equal(first.paper_daily_run_id,null);
    assert.equal((await select('snapshots'))[0].snapshot.benchmark_nav_usd,null);
    assert.deepEqual(await query('SELECT * FROM public.paper_daily_runs'),oldRuns);
    assert.deepEqual(await query('SELECT * FROM public.paper_simulations'),oldHeaders);
  });
  await check('Create replay and append replay are no-ops before stale CAS',async()=>{
    assert.deepEqual(await rpc('model_results_create_season',seasonRequest()),header);
    const replay=await rpc('model_results_append_batch',batch());
    assert.equal(replay.no_op,true); assert.equal(replay.head_hash,first.head_hash);
    assert.equal((await select('events')).length,1);
  });
  await check('All A/B/C/D and budget/theoretical namespaces remain separate',async()=>{
    for(const strategy of 'ABCD') for(const mode of ['budget','theoretical']) {
      const id=`isolated-${strategy}-${mode}`;
      await rpc('model_results_create_season',seasonRequest(id,{strategy_key:strategy,mode}));
      await rpc('model_results_append_batch',batch(id));
      assert.equal((await select('events',id)).length,1);
    }
  });
  await check('Missing currency/native NAV, anon owner and changed manifest rejected',async()=>{
    for(const changed of [{base_currency:undefined},{base_currency:'USD',initial_nav_usd:null},{initial_nav_usd:0}]) {
      const request=seasonRequest('bad-season',changed);
      await assert.rejects(()=>rpc('model_results_create_season',request));
    }
    for(const owner of ['','anon',' anon ','anonymous','public']) {
      const request=seasonRequest('bad-owner'); request.owner=owner;
      await assert.rejects(()=>rpc('model_results_create_season',request));
    }
    await assert.rejects(()=>rpc('model_results_create_season',seasonRequest('season-a',{initial_nav_usd:9999})),/immutable season/);
  });
  await check('USD external-entry theory starts at true zero NAV, budget remains positive',async()=>{
    const valid={mode:'theoretical',initial_nav_usd:0,initial_cash_usd:0,funding_rule:'external_100_per_entry',initial_positions:[]};
    const row=await rpc('model_results_create_season',seasonRequest('zero-funded-theory',valid));
    assert.equal(row.manifest.initial_nav_usd,0); assert.equal(row.paper_simulation_id,null);
    for(const invalid of [
      {...valid,mode:'budget'}, {...valid,funding_rule:'initial_fixed'}, {...valid,initial_nav_usd:-1},
      {...valid,initial_cash_usd:100}, {...valid,initial_positions:[{ticker:'AAPL'}]},
      {...valid,base_currency:'EUR',initial_nav_eur:0},
    ]) await assert.rejects(()=>rpc('model_results_create_season',seasonRequest('invalid-zero-theory',invalid)));
  });
  await check('Hashes are checked against exact canonical payload bytes',async()=>{
    const forged=batch(); forged.content_hash='a'.repeat(64); await rejectedUnchanged(forged,/hash mismatch/);
    const changed=batch(); changed.events[0].payload.action='buy'; await rejectedUnchanged(changed,/Canonical JSON/);
    const badEvent=event('forged'); badEvent.event_hash='a'.repeat(64);
    await rejectedUnchanged(batch('season-a',{run_id:'forged',snapshot:null,events:[badEvent]}),/Event hash mismatch/);
    const badSnapshot=revisedSnapshot(); badSnapshot.snapshot_hash='a'.repeat(64);
    await rejectedUnchanged(batch('season-a',{run_id:'forged-snapshot',snapshot:badSnapshot,events:[]}),/Snapshot hash mismatch/);
  });
  await check('Python canonical floats, exponents, Unicode and NULL verify unchanged',async()=>{
    // Same stdlib JSON options as canonical_json(), without requiring pandas
    // or the application runtime in the independent migration CI job.
    const code=`import json,hashlib
def canon(v): return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(v): return hashlib.sha256(canon(v).encode('utf-8')).hexdigest()
m={'season_key':'float-qa','strategy_key':'B','mode':'theoretical','start_date':'2026-10-20','base_currency':'USD','initial_nav_usd':100.0,'epsilon':1e-7,'title':'á 🚀'}
e={'event_id':'python-float','event_type':'observation','occurred_at':'2026-10-20T20:00:00.000000+00:00','available_at':'2026-10-20T20:00:00.000000+00:00','payload':{'price':1e-7,'missing':None}}
e={**e,'event_hash':digest(e),'event_canonical':canon(e)}
p={'available_at':'2026-10-20T20:00:00.000000+00:00','nav_usd':100.0,'missing':None}
s={'owner':'alice','namespace':'model-original-v1','season_id':'python-floats','manifest':m,'manifest_hash':digest(m),'manifest_canonical':canon(m),'paper_header':None}
b={'owner':'alice','namespace':'model-original-v1','season_id':'python-floats','run_id':'python','market_date':'2026-10-20','events':[e],'snapshot':{'payload':p,'available_at':p['available_at'],'snapshot_hash':digest(p),'snapshot_canonical':canon(p)},'paper_snapshot':None,'paper_snapshot_canonical':None}
b={**b,'content_hash':digest(b),'content_canonical':canon(b),'batch_id':digest(b),'expected_previous_hash':'0'*64}
print(json.dumps([s,b],ensure_ascii=False))`;
    const result=spawnSync(process.env.PYTHON_BIN || 'python3',['-c',code],{cwd:repo,encoding:'utf8'});
    assert.equal(result.status,0,result.stderr);
    const [s,b]=JSON.parse(result.stdout); await rpc('model_results_create_season',s); await rpc('model_results_append_batch',b);
    assert.equal((await select('snapshots','python-floats'))[0].snapshot.missing,null);
  });
  let revision;
  await check('Global event identity deduplicates across runs while same-day snapshots append',async()=>{
    revision=await rpc('model_results_append_batch',batch('season-a',{run_id:'daily-revision-2',snapshot:revisedSnapshot(),expected_previous_hash:first.head_hash}));
    assert.equal(revision.inserted_events,0); assert.equal((await select('snapshots','season-a')).length,2);
    assert.equal((await select('events','season-a','alice','daily-revision-2')).length,0);
    await rejectedUnchanged(batch('season-a',{run_id:'daily-revision-3',snapshot:null,events:[event('decision-1',{payload:{action:'buy'}})],expected_previous_hash:revision.head_hash}),/immutable event/);
  });
  await check('Changed immutable snapshots, batch IDs and run dates reject atomically',async()=>{
    await rejectedUnchanged(batch('season-a',{snapshot:revisedSnapshot(12000),expected_previous_hash:revision.head_hash}),/snapshot is immutable/);
    const reused=batch('season-a',{events:[event('other')],snapshot:null,batch_id:first.batch_id,expected_previous_hash:revision.head_hash});
    await rejectedUnchanged(reused,/immutable batch/);
    await rejectedUnchanged(batch('season-a',{market_date:'2026-10-21',events:[event('other')],snapshot:null,expected_previous_hash:revision.head_hash}),/different market date/);
  });
  await check('Two conceptual competing writers from same head permit only one commit',async()=>{
    const staleHead=revision.head_hash;
    const winner=await rpc('model_results_append_batch',batch('season-a',{snapshot:null,events:[event('winner')],expected_previous_hash:staleHead}));
    await rejectedUnchanged(batch('season-a',{snapshot:null,events:[event('loser')],expected_previous_hash:staleHead}),/Stale model ledger/);
    assert.equal(winner.previous_hash,staleHead); revision=winner;
  });
  await check('Timezone awareness, causal clocks and monotonic availability enforced',async()=>{
    for(const overrides of [{available_at:'2026-10-20T20:00:00'},{occurred_at:'2026-10-20T20:00:00'},{occurred_at:'2026-10-20T20:00:01Z'}]) {
      await rejectedUnchanged(batch('season-a',{snapshot:null,events:[event('bad-clock',overrides)],expected_previous_hash:revision.head_hash}));
    }
    await rejectedUnchanged(batch('season-a',{snapshot:null,events:[event('backwards',{occurred_at:'2026-10-20T19:00:00Z',available_at:'2026-10-20T19:00:00Z'})],expected_previous_hash:revision.head_hash}),/backwards/);
  });
  await check('Unknown event type and credential/recipient PII rejected without writes',async()=>{
    for(const overrides of [{event_type:'real_trade'},{payload:{smtp_password:'secret'}},{payload:{recipient:'redacted'}},{payload:{message:'person@example.org'}}]) {
      await rejectedUnchanged(batch('season-a',{snapshot:null,events:[event('bad-payload',overrides)],expected_previous_hash:revision.head_hash}));
    }
  });
  await check('Owner-scoped reads and nonexistent cross-owner season writes are isolated',async()=>{
    assert.deepEqual(await select('events','season-a','bob'),[]);
    assert.deepEqual(await select('seasons',null,'bob'),[]);
    await rejectedUnchanged(batch('season-a',{owner:'bob',snapshot:null,events:[event('bob')]}),/this owner/);
  });
  await check('SMTP acceptance needs actual dated receipt, never inferred from snapshot',async()=>{
    for(const payload of [{delivery_id:'mail',status:'delivered'},{delivery_id:'mail',status:'smtp_accepted',smtp_code:250}]) {
      await rejectedUnchanged(batch('season-a',{snapshot:null,events:[event('bad-mail',{event_type:'email_delivery',payload})],expected_previous_hash:revision.head_hash}));
    }
    const at='2026-10-20T20:01:00Z';
    const delivered=event('mail-accepted',{event_type:'email_delivery',occurred_at:at,available_at:at,
      payload:{delivery_id:'mail',status:'smtp_accepted',smtp_code:250,receipt_hash:'a'.repeat(64),accepted_at:at}});
    revision=await rpc('model_results_append_batch',batch('season-a',{snapshot:null,events:[delivered],expected_previous_hash:revision.head_hash}));
    assert.equal((await select('events','season-a')).filter(row=>row.event_type==='email_delivery').length,1);
    assert.equal((await select('snapshots','season-a')).length,2);
  });
  await check('Caller transaction rollback leaves no half batch/event/snapshot',async()=>{
    const before=await counts();
    await db.exec('BEGIN');
    await rpc('model_results_append_batch',batch('season-a',{run_id:'transactional',events:[],snapshot:revisedSnapshot(11000,'2026-10-20T20:02:00Z'),expected_previous_hash:revision.head_hash}));
    await db.exec('ROLLBACK'); assert.deepEqual(await counts(),before);
  });
  await db.exec('RESET ROLE');
  await check('Append-only triggers reject mutations even for table owner',async()=>{
    for(const table of ['model_result_seasons','model_result_batches','model_result_events','model_result_snapshots']) {
      for(const statement of [`UPDATE public.${table} SET owner=owner`,`DELETE FROM public.${table}`,`TRUNCATE public.${table} CASCADE`]) {
        await assert.rejects(()=>query(statement),/append-only/);
      }
    }
  });
  let euro;
  await db.exec('SET ROLE service_role');
  await check('Explicit EUR projection is optional, atomic and linked to native header',async()=>{
    const request=seasonRequest('euro',{base_currency:'EUR',initial_nav_eur:10000});
    request.paper_header={name:'EUR original-model projection',status:'active',base_currency:'EUR',benchmark_ticker:'SPY',
      strategy_key:`${ns}:A:budget`,engine_version:ns,start_date:'2026-10-20',initial_nav_eur:10000,initial_cash_eur:10000,
      source_snapshot_date:null,assumptions_json:'{}',initial_positions_json:'[]',created_at:'2026-10-20T20:00:00Z',updated_at:'2026-10-20T20:00:00Z'};
    euro=await rpc('model_results_create_season',request); assert(euro.paper_simulation_id);
    const paper={signal_as_of:'2026-10-20T20:00:00Z',input_hash:'euro-input',positions_after:[],cash_eur:10000,net_nav_eur:10000,hold_nav_eur:10000,benchmark_nav_eur:10000};
    const requestBatch=batch('euro',{paper_snapshot:paper,paper_snapshot_canonical:canonical(paper)});
    const result=await rpc('model_results_append_batch',requestBatch); assert(result.paper_daily_run_id);
    const before=await query('SELECT * FROM public.paper_daily_runs WHERE simulation_id=$1',[euro.paper_simulation_id]);
    await rpc('model_results_append_batch',batch('euro',{paper_snapshot:paper,paper_snapshot_canonical:canonical(paper),events:[event('later')],expected_previous_hash:result.head_hash}));
    assert.deepEqual(await query('SELECT * FROM public.paper_daily_runs WHERE simulation_id=$1',[euro.paper_simulation_id]),before);
  });
  await check('USD projection requires sourced observed FX and checked native conversion',async()=>{
    const request=seasonRequest('usd-fx',{
      fx_observation:{observation_id:'initial-fx',rate_usd_to_eur:0.9,available_at:'2026-10-20T19:00:00Z',source:'offline-fixture'},
      paper_header:{initial_nav_eur:9000,initial_cash_eur:9000,fx_observation_id:'initial-fx'}});
    request.paper_header={name:'USD observed FX projection',status:'active',base_currency:'EUR',benchmark_ticker:'SPY',
      strategy_key:`${ns}:A:budget`,engine_version:ns,start_date:'2026-10-20',initial_nav_eur:9000,initial_cash_eur:9000,
      source_snapshot_date:null,assumptions_json:'{}',initial_positions_json:'[]',created_at:'2026-10-20T20:00:00Z',updated_at:'2026-10-20T20:00:00Z'};
    const wrong=clone(request); wrong.paper_header.initial_nav_eur=10000;
    await assert.rejects(()=>rpc('model_results_create_season',wrong),/does not match/);
    await rpc('model_results_create_season',request);
    const payload={...batch().snapshot.payload,hold_nav_usd:10000,benchmark_nav_usd:10000};
    const snap={payload,available_at:payload.available_at,snapshot_hash:sha(canonical(payload)),snapshot_canonical:canonical(payload)};
    const paper={signal_as_of:'2026-10-20T20:00:00Z',input_hash:'usd-fx',positions_after:[],cash_eur:9000,
      net_nav_eur:9000,hold_nav_eur:9000,benchmark_nav_eur:9000,fx_observation_id:'daily-fx'};
    const fx=event('daily-fx',{event_type:'observation',payload:{rate_usd_to_eur:0.9,source:'offline-fixture'}});
    await rejectedUnchanged(batch('usd-fx',{snapshot:snap,paper_snapshot:paper,paper_snapshot_canonical:canonical(paper)}),/prior observed FX/);
    for(const bad of [{...paper,net_nav_eur:10000},{...paper,net_nav_eur:'NaN'}]) {
      await rejectedUnchanged(batch('usd-fx',{snapshot:snap,events:[fx],paper_snapshot:bad,paper_snapshot_canonical:canonical(bad)}));
    }
    const result=await rpc('model_results_append_batch',batch('usd-fx',{snapshot:snap,events:[fx],paper_snapshot:paper,paper_snapshot_canonical:canonical(paper)}));
    assert.equal((await query('SELECT net_nav_eur FROM public.paper_daily_runs WHERE id=$1',[result.paper_daily_run_id]))[0].net_nav_eur,9000);
    assert.equal((await select('snapshots','usd-fx'))[0].snapshot.net_nav_usd,10000);
  });
  await check('Same-day snapshot revisions are ordered by commit, not lexical run ID',async()=>{
    await rpc('model_results_create_season',seasonRequest('ordered-revisions'));
    const a=await rpc('model_results_append_batch',batch('ordered-revisions',{run_id:'zzz-first'}));
    await rpc('model_results_append_batch',batch('ordered-revisions',{run_id:'aaa-second',events:[],snapshot:revisedSnapshot(11000),expected_previous_hash:a.head_hash}));
    const rows=await select('snapshots','ordered-revisions');
    assert.equal(rows.at(-1).run_id,'aaa-second');
    assert(rows.at(-1).commit_sequence>rows[0].commit_sequence);
  });
  await check('Model paper projection is immutable but old laboratory updates still work',async()=>{
    await assert.rejects(()=>query(`UPDATE public.paper_simulations SET status='paused' WHERE id=$1`,[euro.paper_simulation_id]),/append-only/);
    await assert.rejects(()=>query('UPDATE public.paper_daily_runs SET net_nav_eur=999 WHERE simulation_id=$1',[euro.paper_simulation_id]),/append-only/);
    await query(`UPDATE public.paper_simulations SET status='paused' WHERE id=$1`,[legacy]);
    assert.equal((await query('SELECT status FROM public.paper_simulations WHERE id=$1',[legacy]))[0].status,'paused');
  });
  await db.exec('RESET ROLE');
  await check('Failure after EUR paper insert rolls back the paper and ledger together',async()=>{
    await db.exec(`CREATE FUNCTION public.qa_model_crash() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'simulated crash'; END $$;
      CREATE TRIGGER qa_model_crash BEFORE INSERT ON public.model_result_events FOR EACH ROW EXECUTE FUNCTION public.qa_model_crash();`);
    await db.exec('SET ROLE service_role');
    const before=await counts(), paperBefore=await query('SELECT * FROM public.paper_daily_runs');
    const paper={signal_as_of:'2026-10-21T20:00:00Z',input_hash:'rollback',positions_after:[],cash_eur:10000,net_nav_eur:10000,hold_nav_eur:10000,benchmark_nav_eur:10000};
    const head=(await select('batches','euro')).at(-1).head_hash;
    await assert.rejects(()=>rpc('model_results_append_batch',batch('euro',{run_id:'crash',market_date:'2026-10-21',events:[event('crash')],
      snapshot:revisedSnapshot(),paper_snapshot:paper,paper_snapshot_canonical:canonical(paper),expected_previous_hash:head})),/simulated crash/);
    assert.deepEqual(await counts(),before); assert.deepEqual(await query('SELECT * FROM public.paper_daily_runs'),paperBefore);
    await db.exec('RESET ROLE; DROP TRIGGER qa_model_crash ON public.model_result_events; DROP FUNCTION public.qa_model_crash();');
  });
  await check('Anonymous and authenticated roles cannot invoke RPC or select ledger',async()=>{
    for(const role of ['anon','authenticated']) {
      await db.exec(`SET ROLE ${role}`);
      await assert.rejects(()=>rpc('model_results_select',{owner:'alice',namespace:ns,kind:'events'}),error=>error.code==='42501');
      await assert.rejects(()=>query('SELECT * FROM public.model_result_events'),error=>error.code==='42501');
      await db.exec('RESET ROLE');
    }
  });
  await check('Migration replay preserves every immutable request and native NULL',async()=>{
    const before=await counts(), batchesBefore=await select('batches');
    await db.exec(migration); assert.deepEqual(await counts(),before); assert.deepEqual(await select('batches'),batchesBefore);
  });
  process.stdout.write(`\n${passed} PostgreSQL model-ledger checks passed (in-memory PGlite; no remote DB).\n`);
} finally { await db.close(); }
