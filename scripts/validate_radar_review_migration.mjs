// Offline PostgreSQL tests; uses no credentials or hosted database.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const {PGlite} = await import(process.env.PGLITE_MODULE || '@electric-sql/pglite');
const repo = process.argv[2] || path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const migration = readFileSync(path.join(repo, 'supabase/migrations/20261109110626_radar_review_checkpoints.sql'), 'utf8');
const db = new PGlite();
const table = 'public.radar_review_checkpoints';
const signature = 'public.save_radar_review_checkpoint(text,integer,jsonb)';
const query = async (sql, params = []) => (await db.query(sql, params)).rows;
const save = async (owner, expected, checkpoint) => (await query(
  'SELECT public.save_radar_review_checkpoint($1,$2,$3::jsonb) AS accepted',
  [owner, expected, JSON.stringify(checkpoint)],
))[0].accepted;
const read = (owner) => query(
  'SELECT revision,checkpoint FROM public.radar_review_checkpoints WHERE owner=$1', [owner],
);

try {
  await db.waitReady;
  await db.exec(`
    CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT ALL ON TABLES TO anon, authenticated, service_role;
  `);
  await db.exec(migration);
  await db.exec(migration);
  assert.equal((await query('SELECT relrowsecurity FROM pg_class WHERE oid=$1::regclass', [table]))[0].relrowsecurity, true);
  const rpc = (await query('SELECT prosecdef,proconfig FROM pg_proc WHERE oid=$1::regprocedure', [signature]))[0];
  assert.equal(rpc.prosecdef, false);
  assert.ok(rpc.proconfig.includes('search_path=pg_catalog'));
  for (const role of ['anon', 'authenticated', 'service_role']) {
    for (const permission of ['SELECT', 'INSERT', 'UPDATE', 'DELETE']) {
      const actual = (await query('SELECT has_table_privilege($1,$2,$3) AS allowed', [role, table, permission]))[0].allowed;
      assert.equal(actual, role === 'service_role' && permission !== 'DELETE');
    }
    assert.equal((await query(
      "SELECT has_function_privilege($1,$2,'EXECUTE') AS allowed", [role, signature],
    ))[0].allowed, role === 'service_role');
  }
  process.stdout.write('PASS repeatable migration, invoker RPC and least privileges\n');

  await db.exec('SET ROLE service_role');
  assert.equal(await save('synthetic-alice', 1, {progress: 3}), false);
  assert.equal(await save('synthetic-alice', null, {progress: 3}), true);
  assert.equal(await save('synthetic-alice', null, {progress: 0}), false);
  assert.equal(await save('synthetic-bob', null, {progress: 1}), true);
  assert.equal(await save('synthetic-alice', 1, {progress: 8}), true);
  assert.equal(await save('synthetic-alice', 1, {progress: 4}), false);
  assert.deepEqual(await read('synthetic-alice'), [{revision: 2, checkpoint: {progress: 8}}]);
  assert.deepEqual(await read('synthetic-bob'), [{revision: 1, checkpoint: {progress: 1}}]);
  assert.deepEqual(await read('synthetic-none'), []);
  process.stdout.write('PASS one current revision per owner; stale writers cannot overwrite it\n');

  for (const malformed of [[], null, {owner: 'synthetic-bob'}, {revision: 99}, {large: 'x'.repeat(262144)}]) {
    await assert.rejects(() => save('synthetic-alice', 2, malformed), /constraint/i);
  }
  await assert.rejects(() => save('synthetic-alice', 0, {}), /Invalid Radar checkpoint revision/i);
  await assert.rejects(() => save('synthetic-alice', 2147483647, {}), /Invalid Radar checkpoint revision/i);
  await assert.rejects(() => save('synthetic&owner=eq.bob', null, {}), /constraint/i);
  assert.deepEqual(await read('synthetic-alice'), [{revision: 2, checkpoint: {progress: 8}}]);
  process.stdout.write('PASS malformed, unbounded and spoofed checkpoints leave valid state untouched\n');

  for (const role of ['anon', 'authenticated']) {
    await db.exec('RESET ROLE; SET ROLE ' + role);
    await assert.rejects(() => read('synthetic-alice'), /permission denied/i);
    await assert.rejects(() => save('synthetic-alice', 2, {progress: 0}), /permission denied/i);
  }
  process.stdout.write('PASS browser roles cannot read checkpoints or execute writes\n');
} finally {
  await db.close();
}
