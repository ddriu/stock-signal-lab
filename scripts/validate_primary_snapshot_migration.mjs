// Disposable PostgreSQL only: never reads credentials or connects remotely.
import assert from 'node:assert/strict';
import {readFileSync, readdirSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const {PGlite} = await import(process.env.PGLITE_MODULE || '@electric-sql/pglite');
const root = process.argv[2] || path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const filename = readdirSync(root+'/supabase/migrations').find(x=>x.endsWith('_allow_primary_only_analysis.sql'));
assert(filename, 'Primary observation migration missing');
const migration = readFileSync(root+'/supabase/migrations/'+filename, 'utf8');
const schema = readFileSync(root+'/supabase/schema.sql', 'utf8');
const roles = 'CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;';
const db = new PGlite();
const query = async (text, args=[]) => (await db.query(text,args)).rows;
const rows = () => query('SELECT * FROM public.analysis_snapshots ORDER BY id');
let passed=0;
async function check(name,fn) {await fn(); passed++; process.stdout.write('PASS '+name+'\n');}
async function apply() {await db.exec('BEGIN;'+migration+'COMMIT;');}
try {
  await db.waitReady;
  await db.exec(roles);
  await db.exec(schema);
  // Recreate the exact legacy restriction before seeding historical rows.
  await db.exec('ALTER TABLE public.analysis_snapshots ALTER COLUMN opportunity_score SET NOT NULL');
  await query(`INSERT INTO public.analysis_snapshots
    (owner,ticker,analyzed_at,price,opportunity_score,entry_score,note,created_at)
    VALUES ('alice','OLD','2026-10-30T00:00:00Z',100,75,85,'original note','2026-10-31T01:00:00Z'),
           ('bob','ZERO','2026-10-30T00:00:00Z',20,0,0,'valid zero','2026-10-31T01:00:00Z')`);
  const before = await rows();
  await check('Legacy schema rejects NULL comparison before migration', async()=>{
    await assert.rejects(()=>query(`INSERT INTO public.analysis_snapshots
      (owner,ticker,analyzed_at,price,opportunity_score,entry_score)
      VALUES ('alice','NO','2026-11-03',100,NULL,85)`),e=>e.code==='23502');
    assert.deepEqual(await rows(),before);
  });
  await check('Migration is idempotent and preserves all legacy values', async()=>{
    await apply(); await apply();
    assert.deepEqual(await rows(),before);
    assert.equal((await query(`SELECT is_nullable FROM information_schema.columns
      WHERE table_schema='public' AND table_name='analysis_snapshots' AND column_name='opportunity_score'`))[0].is_nullable,'YES');
  });
  await check('Primary score and observation time survive a NULL comparison', async()=>{
    await db.exec('SET ROLE service_role');
    const row=(await query(`INSERT INTO public.analysis_snapshots
      (owner,ticker,analyzed_at,price,opportunity_score,entry_score,note,created_at)
      VALUES ('alice','PRIMARY','2026-11-03',105,NULL,85,'independent policy','2026-11-04T18:00:00Z')
      RETURNING opportunity_score,entry_score,note,created_at`))[0];
    assert.equal(row.opportunity_score,null);
    assert.equal(row.entry_score,85);
    assert.equal(row.note,'independent policy');
    assert.equal(new Date(row.created_at).toISOString(),'2026-11-04T18:00:00.000Z');
    await db.exec('RESET ROLE');
  });
  await check('Existing valid zero is not converted into unknown',async()=>{
    assert.equal((await query("SELECT opportunity_score FROM public.analysis_snapshots WHERE ticker='ZERO'"))[0].opportunity_score,0);
  });
  await check('Range checks and required primary technical score remain enforced',async()=>{
    for(const bad of [-1,101]) {
      const current=await rows();
      await assert.rejects(()=>query("UPDATE public.analysis_snapshots SET opportunity_score=$1 WHERE ticker='PRIMARY'",[bad]),e=>e.code==='23514');
      assert.deepEqual(await rows(),current);
    }
    await assert.rejects(()=>query("UPDATE public.analysis_snapshots SET entry_score=NULL WHERE ticker='PRIMARY'"),e=>e.code==='23502');
  });
  await check('Migration preserves private-table grants and RLS',async()=>{
    assert.equal((await query("SELECT relrowsecurity FROM pg_class WHERE oid='public.analysis_snapshots'::regclass"))[0].relrowsecurity,true);
    for(const role of ['anon','authenticated']) {
      for(const permission of ['SELECT','INSERT','UPDATE','DELETE']) {
        assert.equal((await query('SELECT has_table_privilege($1,$2,$3) AS allowed',[role,'public.analysis_snapshots',permission]))[0].allowed,false);
      }
      await db.exec('SET ROLE '+role);
      await assert.rejects(()=>rows(),e=>e.code==='42501');
      await db.exec('RESET ROLE');
    }
    assert.equal((await query("SELECT count(*)::int AS n FROM public.analysis_snapshots WHERE owner='bob'"))[0].n,1);
  });
  await check('Transactional rollback restores the original restriction and rows',async()=>{
    const current=await rows();
    // Use an isolated legacy table and preserve a real historical row.
    const isolated=new PGlite();
    try {
      await isolated.waitReady; await isolated.exec(roles); await isolated.exec(schema);
      await isolated.exec('ALTER TABLE public.analysis_snapshots ALTER COLUMN opportunity_score SET NOT NULL');
      await isolated.exec(`INSERT INTO public.analysis_snapshots
        (owner,ticker,analyzed_at,price,opportunity_score,entry_score,note)
        VALUES ('alice','LEGACY','2026-10-30',100,75,85,'retained note')`);
      const preserved=(await isolated.query('SELECT * FROM public.analysis_snapshots')).rows;
      await isolated.exec('BEGIN;'+migration+'ROLLBACK;');
      const value=(await isolated.query(`SELECT is_nullable FROM information_schema.columns
        WHERE table_schema='public' AND table_name='analysis_snapshots' AND column_name='opportunity_score'`)).rows[0].is_nullable;
      assert.equal(value,'NO');
      assert.deepEqual((await isolated.query('SELECT * FROM public.analysis_snapshots')).rows,preserved);
    } finally {await isolated.close();}
    assert.deepEqual(await rows(),current);
  });
  await check('Fresh consolidated schema applies twice with the nullable contract',async()=>{
    await db.exec(schema); await db.exec(schema);
    assert.equal((await query(`SELECT is_nullable FROM information_schema.columns WHERE table_schema='public'
      AND table_name='analysis_snapshots' AND column_name='opportunity_score'`))[0].is_nullable,'YES');
    assert.equal((await query("SELECT opportunity_score FROM public.analysis_snapshots WHERE ticker='PRIMARY'"))[0].opportunity_score,null);
  });
  process.stdout.write(passed+' primary snapshot PostgreSQL checks passed. No remote database used.\n');
} finally {await db.close();}
