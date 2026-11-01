// Offline PostgreSQL permission/CAS checks. No credentials or hosted database.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const {PGlite} = await import(process.env.PGLITE_MODULE || '@electric-sql/pglite');
const repo = process.argv[2] || path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const migration = readFileSync(path.join(repo, 'supabase/migrations/20261031234204_hourly_market_cache.sql'), 'utf8');
const db = new PGlite();
const table = 'public.market_data_cache';
const signature = 'public.upsert_market_data_cache(text,text,text,timestamp with time zone,jsonb,jsonb)';
const query = async (sql, args = []) => (await db.query(sql, args)).rows;
let count = 0;
async function check(name, body) {
  await body();
  count++;
  process.stdout.write('PASS ' + name + '\n');
}
const base = {
  owner: 'synthetic-alice', kind: 'prices', symbol: 'SYNTHETIC',
  observed: '2001-01-02T00:00:00Z', metadata: {source: 'offline synthetic'},
  payload: {encoding: 'synthetic-test-only', value: 100},
};
async function upsert(changes = {}) {
  const row = {...base, ...changes};
  const result = await query(
    'SELECT public.upsert_market_data_cache($1,$2,$3,$4,$5::jsonb,$6::jsonb) AS accepted',
    [row.owner, row.kind, row.symbol, row.observed,
      JSON.stringify(row.metadata), JSON.stringify(row.payload)],
  );
  return result[0].accepted;
}
const allRows = () => query('SELECT owner,kind,symbol,observed_at::text,metadata,payload FROM public.market_data_cache ORDER BY owner,kind,symbol');

try {
  await db.waitReady;
  await db.exec('CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;');
  await check('Migration applies twice, with RLS enabled and no browser-role table privileges', async () => {
    await db.exec(migration);
    await db.exec(migration);
    assert.equal((await query('SELECT relrowsecurity AS enabled FROM pg_class WHERE oid=$1::regclass', [table]))[0].enabled, true);
    for (const role of ['anon', 'authenticated']) {
      for (const permission of ['SELECT', 'INSERT', 'UPDATE', 'DELETE']) {
        assert.equal((await query('SELECT has_table_privilege($1,$2,$3) AS allowed', [role, table, permission]))[0].allowed, false);
      }
      assert.equal((await query('SELECT has_function_privilege($1,$2,$3) AS allowed', [role, signature, 'EXECUTE']))[0].allowed, false);
    }
    assert.equal((await query('SELECT has_function_privilege($1,$2,$3) AS allowed', ['service_role', signature, 'EXECUTE']))[0].allowed, true);
  });
  await check('RPC is security-invoker with a fixed search path, not a browser bypass', async () => {
    const procedure = (await query('SELECT prosecdef,proconfig FROM pg_proc WHERE oid=$1::regprocedure', [signature]))[0];
    assert.equal(procedure.prosecdef, false);
    assert.ok(procedure.proconfig.includes('search_path=pg_catalog, public'));
    const publicExecute = await query(`SELECT privilege_type FROM pg_proc p,
      LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) acl
      WHERE p.oid=$1::regprocedure AND acl.grantee=0 AND acl.privilege_type='EXECUTE'`, [signature]);
    assert.equal(publicExecute.length, 0);
  });
  await db.exec('SET ROLE service_role');
  await check('Backend RPC accepts a valid observation and owner-scoped reads preserve exact payload', async () => {
    assert.equal(await upsert(), true);
    const rows = await query('SELECT metadata,payload FROM public.market_data_cache WHERE owner=$1 AND symbol=$2', [base.owner, base.symbol]);
    assert.equal(rows.length, 1);
    assert.deepEqual(rows[0], {metadata: base.metadata, payload: base.payload});
    await assert.rejects(() => query('DELETE FROM public.market_data_cache'), /permission denied/i);
  });
  await check('Older CAS writer returns false without replacing the valid observation', async () => {
    const before = await allRows();
    assert.equal(await upsert({observed: '2001-01-01T00:00:00Z', payload: {value: 1}}), false);
    assert.deepEqual(await allRows(), before);
  });
  await check('Newer and equal-time same-owner CAS writes return true', async () => {
    assert.equal(await upsert({observed: '2001-01-03T00:00:00Z', payload: {value: 101}}), true);
    assert.equal(await upsert({observed: '2001-01-03T00:00:00Z', payload: {value: 102}}), true);
    const row = (await query('SELECT payload FROM public.market_data_cache WHERE owner=$1 AND symbol=$2', [base.owner, base.symbol]))[0];
    assert.deepEqual(row.payload, {value: 102});
  });
  await check('Owners share no key or result when backend queries use their explicit scope', async () => {
    assert.equal(await upsert({owner: 'synthetic-bob', observed: '2001-01-01T00:00:00Z', payload: {value: 200}}), true);
    const alice = await query('SELECT owner,payload FROM public.market_data_cache WHERE owner=$1', [base.owner]);
    const bob = await query('SELECT owner,payload FROM public.market_data_cache WHERE owner=$1', ['synthetic-bob']);
    assert.deepEqual(alice, [{owner: base.owner, payload: {value: 102}}]);
    assert.deepEqual(bob, [{owner: 'synthetic-bob', payload: {value: 200}}]);
    assert.equal((await query('SELECT owner FROM public.market_data_cache WHERE owner=$1', ['synthetic-missing'])).length, 0);
    // service_role intentionally bypasses RLS: backend owner filters, not
    // browser auth/JWT claims, are the application's isolation contract here.
  });
  const malformed = [
    ['unknown kind', {kind: 'signals'}],
    ['empty symbol', {symbol: ''}],
    ['symbol query injection', {symbol: 'SYNTHETIC&owner=eq.other'}],
    ['symbol too long', {symbol: 'S'.repeat(65)}],
    ['empty owner', {owner: ''}],
    ['owner query injection', {owner: 'alice&owner=eq.bob'}],
    ['metadata array', {metadata: []}],
    ['payload array', {payload: []}],
    ['null metadata', {metadata: null}],
    ['null payload', {payload: null}],
    ['oversized metadata', {metadata: {value: 'x'.repeat(16_384)}}],
    ['oversized payload', {payload: {value: 'x'.repeat(524_288)}}],
    ['null observation time', {observed: null}],
  ];
  for (const [label, changes] of malformed) {
    await check('Reject ' + label + ' atomically without altering any valid observation', async () => {
      const before = await allRows();
      await assert.rejects(() => upsert({...changes, observed: changes.observed === null ? null : '2001-01-04T00:00:00Z'}), /check constraint|not-null constraint/i);
      assert.deepEqual(await allRows(), before);
    });
  }
  await check('Future observation is rejected by database time without replacing the valid cache', async () => {
    const before = await allRows();
    await assert.rejects(() => query(`SELECT public.upsert_market_data_cache($1,$2,$3,
      clock_timestamp()+interval '6 minutes',$4::jsonb,$5::jsonb)`,
    [base.owner, base.kind, base.symbol, JSON.stringify(base.metadata), JSON.stringify({value: 999})]), /Future cache observation/i);
    assert.deepEqual(await allRows(), before);
  });
  for (const role of ['anon', 'authenticated']) {
    await db.exec('RESET ROLE; SET ROLE ' + role);
    await check(role + ' cannot SELECT, INSERT, UPDATE, DELETE or execute the RPC', async () => {
      await assert.rejects(() => query('SELECT * FROM public.market_data_cache'), /permission denied/i);
      await assert.rejects(() => query(`INSERT INTO public.market_data_cache(owner,kind,symbol,observed_at,metadata,payload)
        VALUES('synthetic-intruder','prices','TEST',clock_timestamp(),'{}','{}')`), /permission denied/i);
      await assert.rejects(() => query("UPDATE public.market_data_cache SET payload='{}'"), /permission denied/i);
      await assert.rejects(() => query('DELETE FROM public.market_data_cache'), /permission denied/i);
      await assert.rejects(() => upsert({owner: 'synthetic-intruder'}), /permission denied/i);
    });
  }
  await db.exec('RESET ROLE; SET ROLE service_role');
  await check('Failed browser writes left only the two original owners and exact accepted values', async () => {
    const rows = await query('SELECT owner,payload FROM public.market_data_cache ORDER BY owner');
    assert.deepEqual(rows, [
      {owner: base.owner, payload: {value: 102}},
      {owner: 'synthetic-bob', payload: {value: 200}},
    ]);
  });
  process.stdout.write(count + ' market-cache PostgreSQL checks passed (offline)\n');
} finally {
  await db.close();
}
