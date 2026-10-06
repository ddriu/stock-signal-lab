import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { PGlite } from '@electric-sql/pglite';

// Run only against a new in-memory PostgreSQL instance; no connection strings.
const repo = process.argv[2];
assert(repo, 'Pass the repository directory.');
const sql = name => readFileSync(`${repo}/supabase/${name}`, 'utf8');
const roles = 'CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;';
const db = new PGlite();
let passed = 0;
async function check(name, fn) {
  try { await fn(); }
  catch (error) { throw new Error(`${name}: ${error.message} [${error.code ?? 'assertion'}]`, {cause:error}); }
  passed++;
  process.stdout.write(`PASS ${name}\n`);
}
async function query(statement, args = []) { return (await db.query(statement, args)).rows; }
async function snapshot() {
  return query('SELECT * FROM public.portfolio_snapshots ORDER BY owner, snapshot_date, platform, asset_name');
}
async function replace(owner, date, positions) {
  const rows = await query('SELECT public.replace_portfolio_snapshot($1, $2::date, $3::jsonb, $4) AS inserted',
    [owner, date, positions === null ? null : JSON.stringify(positions), 'qa']);
  return rows[0].inserted;
}
async function rejectsUnchanged(owner, date, positions, code) {
  const before = await snapshot();
  await assert.rejects(() => replace(owner, date, positions), err => !code || err.code === code);
  assert.deepEqual(await snapshot(), before, 'A rejected replacement changed existing positions');
}
const pos = (asset, value = 125) => ({platform: 'Test Broker', asset_name: asset,
  analysis_ticker: asset, quantity: 2, current_price: 62.5, currency: 'EUR', value_eur: value});
const isTarget = row => row.owner === 'alice' &&
  (row.snapshot_date instanceof Date ? row.snapshot_date.toISOString().slice(0,10) : row.snapshot_date) === '2026-10-02';

try {
  await db.waitReady;
  process.stdout.write(`${(await query('SELECT version() AS version'))[0].version}\n`);
  await db.exec(roles);
  await db.exec(sql('migration_portfolio_snapshots.sql'));
  await db.exec(sql('migration_paper_simulation.sql'));
  // Seed legacy data before the new migrations are applied.
  const sim = (await query(`INSERT INTO public.paper_simulations
    (owner,name,strategy_key,engine_version,start_date,initial_nav_eur)
    VALUES ('alice','QA','baseline','qa','2026-10-01',1000) RETURNING id`))[0].id;
  await query(`INSERT INTO public.paper_daily_runs
    (owner,simulation_id,market_date,input_hash,cash_eur,net_nav_eur,hold_nav_eur,
     benchmark_nav_eur,engine_version)
    VALUES ('alice',$1,'2026-10-01','legacy',0,1000,1000,1000,'qa')`, [sim]);

  await check('Both migrations apply and are idempotent', async () => {
    for (let i = 0; i < 2; i++) {
      await db.exec(sql('migration_atomic_portfolio_snapshot.sql'));
      await db.exec(sql('migration_paper_comparator_coverage.sql'));
    }
  });
  await check('Legacy coverage remains NULL and legacy data survives', async () => {
    const row = (await query('SELECT input_hash, hold_coverage_pct, benchmark_coverage_pct FROM public.paper_daily_runs'))[0];
    assert.deepEqual(row, {input_hash: 'legacy', hold_coverage_pct: null, benchmark_coverage_pct: null});
  });
  await check('Coverage accepts NULL, 0, 100 and intermediate values', async () => {
    for (const [hold, benchmark] of [[null,null],[0,100],[100,0],[50.25,99.9]]) {
      await query('UPDATE public.paper_daily_runs SET hold_coverage_pct=$1, benchmark_coverage_pct=$2', [hold, benchmark]);
      assert.deepEqual((await query('SELECT hold_coverage_pct, benchmark_coverage_pct FROM public.paper_daily_runs'))[0],
        {hold_coverage_pct: hold, benchmark_coverage_pct: benchmark});
    }
  });
  await check('Coverage rejects below 0 and above 100 without changing data', async () => {
    for (const column of ['hold_coverage_pct', 'benchmark_coverage_pct']) {
      for (const bad of [-0.01,100.01]) {
        const before = await query('SELECT * FROM public.paper_daily_runs');
        await assert.rejects(() => query(`UPDATE public.paper_daily_runs SET ${column}=$1`, [bad]), err => err.code === '23514');
        assert.deepEqual(await query('SELECT * FROM public.paper_daily_runs'), before);
      }
    }
  });

  await db.exec('SET ROLE service_role');
  await replace('alice', '2026-10-01', [pos('HISTORICAL')]);
  await replace('alice', '2026-10-02', [pos('OLD')]);
  await replace('bob', '2026-10-02', [pos('BOB')]);
  const untouched = (await snapshot()).filter(row => !isTarget(row));
  await check('Valid replacement under service_role; advisory-lock function executes', async () => {
    assert.equal(await replace('alice', '2026-10-02', [pos('AAA'), pos('BBB',250)]), 2);
    assert.deepEqual((await snapshot()).filter(row => !isTarget(row)), untouched);
  });
  await check('Full snapshot replay is content-idempotent', async () => {
    const stable = rows => rows.map(({id,created_at,updated_at,...row}) => row);
    const before = stable(await snapshot());
    assert.equal(await replace('alice','2026-10-02',[pos('AAA'),pos('BBB',250)]),2);
    assert.deepEqual(stable(await snapshot()), before);
  });
  await check('JSON cannot override RPC owner or date', async () => {
    const injected = {...pos('SAFE'), owner:'bob', snapshot_date:'1999-01-01'};
    assert.equal(await replace('alice','2026-10-02',[injected]),1);
    assert.deepEqual((await snapshot()).filter(row => !isTarget(row)), untouched);
  });
  await check('Empty, NULL and malformed payload rejected before mutation', async () => {
    for (const bad of [[],null,{},[null],[5],[{}],[{...pos('BAD'),platform:' '}],[{...pos(' ')}]]) {
      await rejectsUnchanged('alice','2026-10-02',bad);
    }
    await rejectsUnchanged('', '2026-10-02', [pos('X')]);
    await rejectsUnchanged(null, '2026-10-02', [pos('X')]);
    await rejectsUnchanged('alice', null, [pos('X')]);
  });
  await check('Duplicate INSERT rolls back preceding DELETE', async () => {
    await rejectsUnchanged('alice','2026-10-02',[pos('DUP'),pos('DUP')], '23505');
  });
  await check('Invalid second row rolls back the complete replacement', async () => {
    for (const bad of [
      {...pos('BAD'),value_eur:-1},
      {...pos('BAD'),quantity:-1},
      {...pos('BAD'),current_price:-1},
      {...pos('BAD'),currency:'US'},
      {...pos('BAD'),cost_estimate_eur:-1},
    ]) await rejectsUnchanged('alice','2026-10-02',[pos('GOOD'),bad], '23514');
    await rejectsUnchanged('alice','2026-10-02',[pos('GOOD'),{platform:'Broker',asset_name:'MISSING_VALUE'}], '23502');
    await rejectsUnchanged('alice','2026-10-02',[pos('GOOD'),{...pos('BAD'),quantity:'not-a-number'}], '22P02');
  });
  await check('Successful RPC participates in caller transaction and rollback', async () => {
    const before = await snapshot();
    await db.exec('BEGIN');
    assert.equal(await replace('alice','2026-10-02',[pos('TRANSIENT')]),1);
    await db.exec('ROLLBACK');
    assert.deepEqual(await snapshot(), before);
  });
  await db.exec('RESET ROLE');
  await check('RPC grants exclude PUBLIC, anon and authenticated', async () => {
    const sig = 'public.replace_portfolio_snapshot(text,date,jsonb,text)';
    for (const role of ['anon','authenticated','service_role']) {
      assert.equal((await query('SELECT has_function_privilege($1,$2,\'EXECUTE\') AS allowed',[role,sig]))[0].allowed,
        role === 'service_role');
    }
    const proc = (await query(`SELECT prosecdef, proconfig FROM pg_proc
      WHERE oid = 'public.replace_portfolio_snapshot(text,date,jsonb,text)'::regprocedure`))[0];
    assert.equal(proc.prosecdef,false);
    assert(proc.proconfig.includes('search_path=public, pg_temp'));
  });
  await check('anon and authenticated cannot invoke RPC or read snapshots', async () => {
    for (const role of ['anon','authenticated']) {
      await db.exec(`SET ROLE ${role}`);
      await assert.rejects(() => replace('alice','2026-10-02',[pos('NO')]),err => err.code==='42501');
      await assert.rejects(() => query('SELECT * FROM public.portfolio_snapshots'),err => err.code==='42501');
      await db.exec('RESET ROLE');
    }
  });
  await check('Reapplying migrations preserves all seeded data', async () => {
    const before = await snapshot();
    const paperBefore = await query('SELECT * FROM public.paper_daily_runs');
    await db.exec(sql('migration_atomic_portfolio_snapshot.sql'));
    await db.exec(sql('migration_paper_comparator_coverage.sql'));
    assert.deepEqual(await snapshot(), before);
    assert.deepEqual(await query('SELECT * FROM public.paper_daily_runs'), paperBefore);
  });
  await check('Fresh full schema applies twice and contains both migration features', async () => {
    const fresh = new PGlite();
    try {
      await fresh.waitReady;
      await fresh.exec(roles);
      await fresh.exec(sql('schema.sql'));
      await fresh.exec(sql('schema.sql'));
      const columns = (await fresh.query(`SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name='paper_daily_runs' AND column_name LIKE '%coverage_pct'`)).rows;
      assert.deepEqual(columns.map(row => row.column_name).sort(),
        ['benchmark_coverage_pct','coverage_pct','hold_coverage_pct']);
      await fresh.exec('SET ROLE service_role');
      const count = (await fresh.query('SELECT public.replace_portfolio_snapshot($1,$2::date,$3::jsonb) AS inserted',
        ['fresh','2026-10-02',JSON.stringify([pos('FRESH')])])).rows[0].inserted;
      assert.equal(count,1);
    } finally { await fresh.close(); }
  });
  const bundle = sql('release_stabilization_20261005.sql');
  await check('Release bundle contains the two exact migrations', async () => {
    assert(bundle.includes(sql('migration_atomic_portfolio_snapshot.sql')));
    assert(bundle.includes(sql('migration_paper_comparator_coverage.sql')));
  });
  await check('Release bundle applies twice and preserves all existing rows', async () => {
    const before = await snapshot();
    const paperBefore = await query('SELECT * FROM public.paper_daily_runs');
    await db.exec(bundle);
    await db.exec(bundle);
    assert.deepEqual(await snapshot(), before);
    assert.deepEqual(await query('SELECT * FROM public.paper_daily_runs'), paperBefore);
  });
  await check('Release bundle aborts before DDL when base tables are missing', async () => {
    const broken = new PGlite();
    try {
      await broken.waitReady;
      await broken.exec(roles);
      await assert.rejects(() => broken.exec(bundle), err => err.code === 'P0001');
      await broken.exec('ROLLBACK');
      assert.equal((await broken.query(`SELECT to_regprocedure('public.replace_portfolio_snapshot(text,date,jsonb,text)') IS NULL AS absent`)).rows[0].absent,true);
    } finally { await broken.close(); }
  });
  await check('Release bundle aborts before DDL on incompatible legacy columns', async () => {
    const broken = new PGlite();
    try {
      await broken.waitReady;
      await broken.exec(roles);
      await broken.exec(sql('migration_portfolio_snapshots.sql'));
      await broken.exec(sql('migration_paper_simulation.sql'));
      await broken.exec('ALTER TABLE public.portfolio_snapshots DROP COLUMN notes');
      await assert.rejects(() => broken.exec(bundle), err => err.code === 'P0001');
      await broken.exec('ROLLBACK');
      assert.equal((await broken.query(`SELECT to_regprocedure('public.replace_portfolio_snapshot(text,date,jsonb,text)') IS NULL AS absent`)).rows[0].absent,true);
      assert.equal((await broken.query(`SELECT count(*) AS count FROM information_schema.columns
        WHERE table_schema='public' AND table_name='paper_daily_runs' AND column_name='benchmark_coverage_pct'`)).rows[0].count,0);
    } finally { await broken.close(); }
  });
  await check('Failed postflight rolls back the entire release bundle', async () => {
    const broken = new PGlite();
    try {
      await broken.waitReady;
      await broken.exec(roles);
      await broken.exec(sql('migration_portfolio_snapshots.sql'));
      await broken.exec(sql('migration_paper_simulation.sql'));
      await broken.exec('ALTER TABLE public.paper_daily_runs ADD COLUMN hold_coverage_pct text');
      await broken.exec(`INSERT INTO public.portfolio_snapshots
        (owner,snapshot_date,platform,asset_name,value_eur) VALUES ('legacy','2026-10-01','Broker','OLD',100)`);
      const before = (await broken.query('SELECT * FROM public.portfolio_snapshots')).rows;
      await assert.rejects(() => broken.exec(bundle), err => err.code === 'P0001');
      await broken.exec('ROLLBACK');
      assert.deepEqual((await broken.query('SELECT * FROM public.portfolio_snapshots')).rows, before);
      assert.equal((await broken.query(`SELECT to_regprocedure('public.replace_portfolio_snapshot(text,date,jsonb,text)') IS NULL AS absent`)).rows[0].absent,true);
      assert.equal((await broken.query(`SELECT count(*) AS count FROM information_schema.columns
        WHERE table_schema='public' AND table_name='paper_daily_runs' AND column_name='benchmark_coverage_pct'`)).rows[0].count,0);
      assert.equal((await broken.query(`SELECT data_type FROM information_schema.columns
        WHERE table_schema='public' AND table_name='paper_daily_runs' AND column_name='hold_coverage_pct'`)).rows[0].data_type,'text');
    } finally { await broken.close(); }
  });
  process.stdout.write(`RESULT: ${passed} PostgreSQL migration checks passed. No external database used.\n`);
} catch (error) {
  process.stderr.write(`FAILED: ${error.message}\n`);
  process.exitCode = 1;
} finally { await db.close(); }
