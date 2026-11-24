// Isolated synthetic Postgres validation; never reads credentials or a backend.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFile, readdir} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const root = process.argv[2] || process.cwd();
const modulePath = process.env.PGLITE_MODULE;
const {PGlite} = modulePath ? await import(pathToFileURL(modulePath).href) : await import('@electric-sql/pglite');
const db = new PGlite();
const names = (await readdir(path.join(root, 'supabase/migrations')))
  .filter(name => name.endsWith('_arena_universe_policy.sql'));
assert.equal(names.length, 1, 'One CLI-generated owner-policy migration is required');
const migration = await readFile(path.join(root, 'supabase/migrations', names[0]), 'utf8');
await db.exec(`
  create role anon; create role authenticated; create role service_role bypassrls;
  create table public.operations(id int, value text);
  insert into public.operations values(1,'preserved');
  create table public.favorites(id int, value text);
  insert into public.favorites values(1,'preserved');
  create schema arena_private;
  create table arena_private.league(id boolean, state jsonb);
  insert into arena_private.league values(true,'{"data_quality_pause":{"paused":true},"rounds":[{"day":"2001-01-01"}]}');
`);
const preservation = async () => ({
  operations: (await db.query('select * from public.operations')).rows,
  favorites: (await db.query('select * from public.favorites')).rows,
  arena: (await db.query('select * from arena_private.league')).rows,
});
const before = await preservation();
await db.exec(migration);
assert.deepEqual(await preservation(), before);
assert.equal((await db.query('select count(*)::int as n from public.arena_universe_policies')).rows[0].n, 0);
assert.equal((await db.query("select relrowsecurity from pg_class where oid='public.arena_universe_policies'::regclass")).rows[0].relrowsecurity, true);

const policy = (owner, mode='verified_only', approved_at='2001-01-02T03:04:05.000000+00:00') => {
  const data = {approved_at, mode, owner, version:'arena-universe-policy-v1'};
  return {...data, policy_id:createHash('sha256').update(JSON.stringify(data)).digest('hex')};
};
const insert = value => db.query(`insert into public.arena_universe_policies(owner,mode,approved_at,version,policy_id)
  values($1,$2,$3,$4,$5)`, [value.owner,value.mode,value.approved_at,value.version,value.policy_id]);

await db.exec('set role service_role');
await insert(policy('synthetic-alice'));
await insert(policy('synthetic-bob','strict',null));
assert.equal((await db.query("select mode from public.arena_universe_policies where owner='synthetic-alice'")).rows[0].mode, 'verified_only');
assert.equal((await db.query("select mode from public.arena_universe_policies where owner='synthetic-bob'")).rows[0].mode, 'strict');
for (const bad of [
  {...policy('bad-mode'),mode:'anything'}, {...policy('bad-consent'),approved_at:null},
  {...policy('bad-version'),version:'future-v2'}, {...policy('bad-identity'),policy_id:'not-a-hash'},
  {...policy('bad-future'),approved_at:'9999-01-01T00:00:00Z'},
  ...['', 'anon', 'anonymous', 'public', 'group', 'grupo_compartido', ' synthetic', 'alice&owner=eq.bob'].map(owner=>policy(owner)),
]) await assert.rejects(() => insert(bad));
await assert.rejects(() => db.query('delete from public.arena_universe_policies'));
await db.exec('reset role');

for (const role of ['anon','authenticated']) {
  await db.exec(`set role ${role}`);
  await assert.rejects(() => db.query('select * from public.arena_universe_policies'));
  await assert.rejects(() => insert(policy('synthetic-intruder')));
  await assert.rejects(() => db.query("update public.arena_universe_policies set mode='strict'"));
  await db.exec('reset role');
}
assert.equal((await db.query('select count(*)::int as n from public.arena_universe_policies')).rows[0].n, 2);
assert.deepEqual(await preservation(), before);
await db.close();
console.log('PASS private policy consent, owner constraints, RLS/grants and unrelated history preservation');
