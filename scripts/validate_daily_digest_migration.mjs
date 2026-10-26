// Offline PostgreSQL check: no credentials or production connections.
import assert from 'node:assert/strict';
import {readFileSync,readdirSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const {PGlite} = await import(process.env.PGLITE_MODULE || '@electric-sql/pglite');
const repo = process.argv[2] || path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const filename = readdirSync(repo+'/supabase/migrations').find(x=>x.endsWith('_daily_score_reports_delivery.sql'));
assert(filename,'Daily-report migration missing');
const migration = readFileSync(repo+'/supabase/migrations/'+filename,'utf8');
const db = new PGlite();
let count = 0;
const query = async (sql,args=[]) => (await db.query(sql,args)).rows;
async function check(name,fn) { await fn(); count++; process.stdout.write('PASS '+name+'\n'); }
const id = 'a'.repeat(64), key='b'.repeat(64);
const report = JSON.stringify({report_id:id,as_of:'2026-10-23',rows:[]});
try {
  await db.waitReady;
  await db.exec('CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;');
  await check('Migration applies twice and exposes no private reports',async()=>{
    await db.exec(migration); await db.exec(migration);
    for(const table of ['daily_score_reports','daily_digest_deliveries']) {
      assert.equal((await query('SELECT relrowsecurity AS enabled FROM pg_class WHERE oid=$1::regclass',['public.'+table]))[0].enabled,true);
      for(const role of ['anon','authenticated']) for(const permission of ['SELECT','INSERT','UPDATE','DELETE']) {
        assert.equal((await query('SELECT has_table_privilege($1,$2,$3) AS allowed',[role,'public.'+table,permission]))[0].allowed,false);
      }
    }
  });
  await db.exec('SET ROLE service_role');
  await check('Backend inserts and reads immutable report',async()=>{
    await query('INSERT INTO public.daily_score_reports(owner,report_id,market_date,report_json) VALUES($1,$2,$3,$4)',['alice',id,'2026-10-23',report]);
    assert.equal((await query('SELECT report_id FROM public.daily_score_reports WHERE owner=$1 AND report_id=$2',['alice',id]))[0].report_id,id);
    assert.equal((await query('SELECT report_id FROM public.daily_score_reports WHERE owner=$1 AND report_id=$2',['bob',id])).length,0);
    await assert.rejects(()=>query('UPDATE public.daily_score_reports SET report_json=$1',[report]),/permission denied/);
    await assert.rejects(()=>query('DELETE FROM public.daily_score_reports'),/permission denied/);
  });
  const reserve=()=>query("INSERT INTO public.daily_digest_deliveries(owner,delivery_key,market_date,report_id) VALUES('alice',$1,'2026-10-23',$2) ON CONFLICT(owner,delivery_key) DO NOTHING RETURNING delivery_key",[key,id]);
  await check('One claim survives replays and uncertain SMTP',async()=>{
    assert.equal((await reserve()).length,1); assert.equal((await reserve()).length,0);
    await query("UPDATE public.daily_digest_deliveries SET status='uncertain' WHERE owner='alice' AND delivery_key=$1 AND status='reserved'",[key]);
    assert.equal((await reserve()).length,0);
    assert.equal((await query("UPDATE public.daily_digest_deliveries SET status='sent' WHERE owner='alice' AND delivery_key=$1 AND status='reserved' RETURNING status",[key])).length,0);
  });
  await check('Claim requires same owner, report and session; errors exclude secrets',async()=>{
    await assert.rejects(()=>query("INSERT INTO public.daily_digest_deliveries(owner,delivery_key,market_date,report_id) VALUES('bob',$1,'2026-10-23',$2)",['c'.repeat(64),id]),/foreign key/);
    await assert.rejects(()=>query("INSERT INTO public.daily_digest_deliveries(owner,delivery_key,market_date,report_id) VALUES('alice',$1,'2026-10-22',$2)",['c'.repeat(64),id]),/foreign key/);
    await assert.rejects(()=>query("UPDATE public.daily_digest_deliveries SET error_type='password=secret'"),/check constraint/);
  });
  await db.exec('RESET ROLE; SET ROLE anon;');
  await check('Anonymous cannot read reports or delivery attempts',async()=>{
    await assert.rejects(()=>query('SELECT * FROM public.daily_score_reports'),/permission denied/);
    await assert.rejects(()=>query('SELECT * FROM public.daily_digest_deliveries'),/permission denied/);
  });
  process.stdout.write(count+' daily-report PostgreSQL checks passed\n');
} finally { await db.close(); }
