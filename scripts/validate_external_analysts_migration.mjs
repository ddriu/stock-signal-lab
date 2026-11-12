// Offline PostgreSQL validation. No hosted project, credentials or mailboxes.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const {PGlite} = await import(process.env.PGLITE_MODULE || '@electric-sql/pglite');
const root = process.argv[2] || path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const sql = readFileSync(path.join(root, 'supabase/migrations/20261112082233_external_analysts.sql'), 'utf8');
const db = new PGlite();
const query = async (sql, args = []) => (await db.query(sql, args)).rows;
const rpc = async (name, owner, payload) => (await query(`SELECT public.${name}($1,$2::jsonb) AS result`, [owner, JSON.stringify(payload)]))[0].result;
const select = (owner, kind) => rpc('external_analyst_select', owner, {kind});
const call = {id: 'a'.repeat(64), provider: 'perito_inversor', source_id: 'synthetic-call', ticker: 'SYN',
  received_at: '2026-11-12T09:00:00Z', observed_at: '2026-11-12T09:01:00Z', entry: 10, stop: 9, target: 13};
const message = {message_key: 'b'.repeat(64), fingerprint: 'c'.repeat(64), provider: 'perito_inversor',
  received_at: call.received_at, observed_at: call.observed_at, announced_count: 3, issues: [],
  calls: [{call, revision_id: 'd'.repeat(64)}]};
const tables = ['messages','calls','revisions','events','settings','email_deliveries','simulation_runs'];
try {
  await db.waitReady;
  await db.exec('CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;');
  await db.exec(sql);
  await db.exec(sql);
  for (const table of tables) {
    const name = 'public.external_analyst_' + table;
    assert.equal((await query('SELECT relrowsecurity FROM pg_class WHERE oid=$1::regclass', [name]))[0].relrowsecurity, true);
    for (const role of ['anon', 'authenticated']) {
      assert.equal((await query("SELECT has_table_privilege($1,$2,'SELECT') AS allowed", [role, name]))[0].allowed, false);
    }
  }
  const signatures = (await query("SELECT p.oid::regprocedure::text AS signature FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.proname LIKE 'external_analyst_%'")).map(row => row.signature);
  assert.ok(signatures.length >= 10);
  for (const signature of signatures) {
    const fn = (await query('SELECT prosecdef,proconfig FROM pg_proc WHERE oid=$1::regprocedure', [signature]))[0];
    assert.equal(fn.prosecdef, false);
    assert.ok(fn.proconfig.includes('search_path=pg_catalog'));
    for (const role of ['anon','authenticated']) {
      assert.equal((await query("SELECT has_function_privilege($1,$2,'EXECUTE') AS allowed", [role, signature]))[0].allowed, false);
    }
  }
  process.stdout.write('PASS repeatable additive migration; RLS, invoker RPC, no browser access\n');
  await db.exec('SET ROLE service_role');
  assert.deepEqual(await rpc('external_analyst_import', 'synthetic-alice', message), {inserted: 1, revised: 0, duplicates: 0, message_duplicate: false});
  assert.deepEqual(await rpc('external_analyst_import', 'synthetic-alice', message), {inserted: 0, revised: 0, duplicates: 1, message_duplicate: true});
  assert.equal((await select('synthetic-bob', 'calls')).length, 0);
  assert.equal((await select('synthetic-alice', 'messages'))[0].announced_count, 3);
  const revised = structuredClone(message);
  revised.message_key = 'e'.repeat(64); revised.fingerprint = 'f'.repeat(64);
  revised.calls[0].call.stop = 9.5; revised.calls[0].revision_id = '1'.repeat(64);
  revised.received_at = revised.calls[0].call.received_at = '2026-11-12T10:00:00Z';
  revised.observed_at = revised.calls[0].call.observed_at = '2026-11-12T10:01:00Z';
  assert.equal((await rpc('external_analyst_import','synthetic-alice',revised)).revised, 1);
  assert.equal((await select('synthetic-alice','revisions')).length, 2);
  const malformed = structuredClone(revised);
  malformed.message_key = '2'.repeat(64); malformed.fingerprint = '3'.repeat(64);
  malformed.calls.push({call: {...call, id: '4'.repeat(64), source_id: 'bad-clock'}, revision_id: '5'.repeat(64)});
  await assert.rejects(() => rpc('external_analyst_import','synthetic-alice',malformed), /Invalid revision clock/);
  assert.equal((await select('synthetic-alice','messages')).length, 2);
  assert.equal((await select('synthetic-alice','calls')).length, 1);
  await assert.rejects(() => rpc('external_analyst_import','synthetic-alice',{...message,fingerprint:'6'.repeat(64)}), /Immutable message/);
  await assert.rejects(() => db.exec("UPDATE public.external_analyst_calls SET source_id='tampered'"), /permission denied|append-only/i);
  process.stdout.write('PASS atomic import, duplicate/revision history, distinct owner, invalid batch rollback\n');
  const event = {call_id:call.id,event_key:'activation-1',occurred_at:'2026-11-12T11:00:00Z',payload:{status:'open'}};
  assert.equal(await rpc('external_analyst_append_event','synthetic-alice',event), true);
  assert.equal(await rpc('external_analyst_append_event','synthetic-alice',event), false);
  await assert.rejects(() => rpc('external_analyst_append_event','synthetic-alice',{...event,payload:{status:'stopped'}}), /Immutable event/);
  await assert.rejects(() => rpc('external_analyst_append_event','synthetic-bob',event), /precedes observation/);
  await assert.rejects(() => rpc('external_analyst_append_event','synthetic-alice',{...event,event_key:'past',occurred_at:'2026-11-12T09:00:00Z'}), /precedes observation/);
  const delivery = {delivery_key:'report-1',report:{calls:1}};
  assert.equal(await rpc('external_analyst_claim_delivery','synthetic-alice',delivery), true);
  assert.equal(await rpc('external_analyst_claim_delivery','synthetic-alice',delivery), false);
  await rpc('external_analyst_finish_delivery','synthetic-alice',{delivery_key:'report-1',status:'uncertain'});
  await assert.rejects(() => rpc('external_analyst_finish_delivery','synthetic-alice',{delivery_key:'report-1',status:'sent'}), /reservation not available/);
  await assert.rejects(() => rpc('external_analyst_claim_delivery','synthetic-alice',{delivery_key:'pii',report:{recipient:'private@example.invalid'}}), /Invalid external delivery/);
  await rpc('external_analyst_settings_write','synthetic-alice',{kind:'cursor',value:{uidvalidity:123,last_uid:7}});
  await rpc('external_analyst_settings_write','synthetic-alice',{kind:'preferences',value:{enabled:true,commission_eur:1}});
  assert.equal((await select('synthetic-alice','settings'))[0].cursor_json.last_uid, 7);
  assert.equal((await select('synthetic-alice','settings'))[0].prefs_json.enabled, true);
  assert.deepEqual(await select('synthetic-bob','settings'), []);
  for (const invalid of [null, {last_uid:null}, {last_uid:true}, {last_uid:-1}, {last_uid:4294967296}, {mailbox_hash:null}, {mailbox_hash:'raw-mailbox'}]) {
    await assert.rejects(() => rpc('external_analyst_settings_write','synthetic-alice',{kind:'cursor',value:invalid}), /Invalid (external settings|cursor UID|mailbox hash)/);
  }
  assert.equal((await select('synthetic-alice','settings'))[0].cursor_json.last_uid, 7);
  await rpc('external_analyst_settings_write','synthetic-bob',{kind:'preferences',value:{enabled:false}});
  assert.equal((await select('synthetic-alice','settings'))[0].prefs_json.enabled, true);
  assert.equal((await select('synthetic-bob','settings'))[0].prefs_json.enabled, false);
  const simulation = {run_id:'day-1',observed_at:'2026-11-12T11:00:00Z',payload:{equity_eur:100}};
  assert.equal(await rpc('external_analyst_save_simulation','synthetic-alice',simulation), true);
  assert.equal(await rpc('external_analyst_save_simulation','synthetic-alice',simulation), false);
  await assert.rejects(() => rpc('external_analyst_save_simulation','synthetic-alice',{...simulation,payload:{equity_eur:200}}), /Immutable simulation/);
  assert.deepEqual(await select('synthetic-bob','simulations'), []);
  process.stdout.write('PASS append-only events, temporal guard, delivery claims, private settings and immutable simulations\n');
  for (const role of ['anon','authenticated']) {
    await db.exec('RESET ROLE; SET ROLE ' + role);
    await assert.rejects(() => select('synthetic-alice','calls'), /permission denied/i);
    await assert.rejects(() => db.exec('SELECT * FROM public.external_analyst_calls'), /permission denied/i);
  }
  process.stdout.write('PASS browser roles cannot read or invoke owner-scoped RPCs\n');
} finally { await db.close(); }
