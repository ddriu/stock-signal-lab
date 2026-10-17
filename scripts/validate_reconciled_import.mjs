import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { PGlite } from '@electric-sql/pglite';

// Synthetic fixtures and in-memory PostgreSQL only. Never read credentials,
// contact a database, or print the private accounting backup in diagnostics.
const roles = 'CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;';
const migrationName = 'migrations/20261015192038_reconciled_portfolio_import.sql';
const signature = 'public.import_reconciled_portfolio(text,jsonb,text)';
const accountingTables = ['operations', 'portfolio_snapshots', 'portfolio_accounts', 'private_investments'];
const marker = '[reconciled-workbook:v1]';
const digest = number => number.toString(16).padStart(64, '0');
const clone = value => structuredClone(value);

function position(changes = {}) {
  return {
    snapshot_date: '2026-10-13', platform: 'Synthetic Broker', asset_name: 'Synthetic asset',
    raw_identifier: '', analysis_ticker: 'AAA', asset_type: 'Acción', portfolio_block: '',
    quantity: 2, current_price: 20, currency: 'EUR', value_eur: 40, return_pct: null,
    cost_estimate_eur: 21, gain_loss_eur: 19, comments: '', source: 'synthetic', notes: '',
    ...changes,
  };
}

function operation(changes = {}) {
  const row = {
    ticker: 'AAA', account_name: 'Synthetic Broker', side: 'Compra', quantity: 2,
    price: 10, fees: 1, executed_at: '2026-10-01T12:00:00+00:00', currency: 'EUR',
    source_event_id: 'synthetic-event', ...changes,
  };
  const local = row.quantity * row.price + (row.side === 'Compra' ? row.fees : -row.fees);
  return {
    settlement_amount_local: local,
    settlement_amount_eur: row.currency === 'EUR' ? local : null,
    fee_eur: row.currency === 'EUR' ? row.fees : null,
    fx_rate_to_eur: row.currency === 'EUR' ? 1 : null,
    notes: marker + JSON.stringify({source_event_id: row.source_event_id,
      phase: 'historical', fee_quality: 'documented', fx_quality: 'exact'}),
    ...row,
  };
}

function bundle(number = 1, changes = {}) {
  return {
    source_sha256: digest(number), snapshot_date: '2026-10-13',
    positions: [position()],
    operations: [operation(), operation({quantity: 1, price: 20,
      executed_at: '2026-10-15T12:00:00+00:00', source_event_id: 'synthetic-post-event'})],
    metadata: {events: [], issues: [], coverage: {capital_flows_complete: false}, inventory: [],
      provenance: {schema: 'reconciled-workbook:v1', source_sha256: digest(number)}},
    ...changes,
  };
}

export async function validateReconciledImport(repo) {
  assert(repo, 'Pass the repository directory.');
  const sql = name => readFileSync(resolve(repo, 'supabase', name), 'utf8');
  const migration = sql(migrationName);
  assert(migration.trim(), 'Reconciled import migration is empty.');
  const schema = sql('schema.sql');
  // The full schema also embeds the migration after release integration. Remove
  // that exact embedded section to exercise an actual pre-migration database.
  const legacySchema = schema.includes(migration) ? schema.replace(migration, '') : schema;
  const db = new PGlite();
  let passed = 0;
  const query = async (statement, args = []) => (await db.query(statement, args)).rows;
  async function check(name, fn) {
    try { await fn(); }
    catch (error) {
      // Do not include assertion diffs, SQL parameters, or stored backup JSON.
      throw new Error(`Reconciled import check failed: ${name} [${error.code ?? 'assertion'}]`);
    }
    passed++;
  }
  async function imported(owner, data, actor = 'synthetic-qa') {
    return (await query('SELECT public.import_reconciled_portfolio($1,$2::jsonb,$3) AS summary',
      [owner, data === null ? null : JSON.stringify(data), actor]))[0].summary;
  }
  async function rows(table, owner) {
    assert([...accountingTables, 'portfolio_import_batches'].includes(table));
    return (await query(`SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY id),'[]'::jsonb) AS items
      FROM public.${table} r WHERE owner=$1`, [owner]))[0].items;
  }
  async function accounting(owner, includeBatches = true) {
    const result = {};
    for (const table of [...accountingTables, ...(includeBatches ? ['portfolio_import_batches'] : [])]) {
      result[table] = await rows(table, owner);
    }
    return result;
  }
  async function rejectsUnchanged(owner, data) {
    const before = await accounting(owner);
    await assert.rejects(() => imported(owner, data));
    assert.deepEqual(await accounting(owner), before);
  }
  async function seedManual(owner) {
    await query(`INSERT INTO public.operations
      (owner,ticker,account_name,side,quantity,price,fees,settlement_amount_eur,fee_eur,
       fx_rate_to_eur,executed_at,notes,currency,recorded_by)
      VALUES ($1,'AAA','Synthetic Broker','Compra',2,10,4,24,4,1,
       '2026-10-01T12:00:00Z','Synthetic manual amount preserved','EUR','manual')`, [owner]);
    await query(`INSERT INTO public.portfolio_snapshots
      (owner,snapshot_date,platform,asset_name,analysis_ticker,quantity,current_price,currency,value_eur,notes)
      VALUES ($1,'2026-10-13','Synthetic Broker','Synthetic asset','AAA',2,20,'EUR',99,
       'Synthetic manual snapshot preserved')`, [owner]);
    await query(`INSERT INTO public.portfolio_accounts
      (owner,account_name,account_type,investments_value,cash_balance,notes)
      VALUES ($1,'Synthetic Broker','Bróker',99,7,'Synthetic account preserved')`, [owner]);
    await query(`INSERT INTO public.private_investments
      (owner,platform,project_name,invested_amount,current_value,start_date,notes)
      VALUES ($1,'Civislend','Synthetic private project',10,11,'2026-01-01',
       'Synthetic private investment preserved')`, [owner]);
  }

  try {
    await db.waitReady;
    await db.exec(roles);
    await db.exec(legacySchema);
    await seedManual('manual-owner');
    await seedManual('unrelated-owner');
    const legacy = {};
    for (const owner of ['manual-owner', 'unrelated-owner']) legacy[owner] = await accounting(owner, false);

    await check('legacy migration is additive and repeatable', async () => {
      await db.exec(migration);
      await db.exec(migration);
      for (const owner of ['manual-owner', 'unrelated-owner']) {
        const current = await accounting(owner, false);
        current.operations = current.operations.map(({source_event_id, settlement_amount_local, ...row}) => {
          assert.equal(source_event_id, null);
          assert.equal(settlement_amount_local, null);
          return row;
        });
        const expected = clone(legacy[owner]);
        expected.operations = expected.operations.map(({source_event_id, settlement_amount_local, ...row}) => row);
        assert.deepEqual(current, expected);
      }
    });

    await check('RPC and batch table have server-only privileges and RLS', async () => {
      for (const role of ['anon', 'authenticated', 'service_role']) {
        assert.equal((await query("SELECT has_function_privilege($1,$2,'EXECUTE') AS allowed",
          [role, signature]))[0].allowed, role === 'service_role');
        for (const privilege of ['SELECT', 'INSERT', 'UPDATE', 'DELETE']) {
          assert.equal((await query('SELECT has_table_privilege($1,$2,$3) AS allowed',
            [role, 'public.portfolio_import_batches', privilege]))[0].allowed, role === 'service_role');
        }
      }
      const proc = (await query('SELECT prosecdef,proconfig FROM pg_proc WHERE oid=$1::regprocedure', [signature]))[0];
      assert.equal(proc.prosecdef, false);
      assert(proc.proconfig.includes('search_path=public, pg_temp'));
      assert.equal((await query("SELECT relrowsecurity FROM pg_class WHERE oid='public.portfolio_import_batches'::regclass"))[0].relrowsecurity, true);
      const publicAccess = await query(`SELECT 1 FROM pg_proc p,
        LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
        WHERE p.oid=$1::regprocedure AND a.grantee=0 AND a.privilege_type='EXECUTE'`, [signature]);
      assert.equal(publicAccess.length, 0);
    });

    await db.exec('SET ROLE service_role');
    await check('complete batch inserts historical and post-snapshot rows once', async () => {
      const data = bundle();
      const first = await imported('basic-owner', data);
      assert.equal(first.inserted_positions, 1);
      assert.equal(first.inserted_operations, 2);
      assert.equal(first.skipped_positions, 0);
      assert.equal(first.skipped_operations, 0);
      assert.equal(first.pending_conflicts, 0);
      assert.equal(first.already_imported, false);
      assert.equal(first.source_sha256, digest(1));
      assert(first.batch_id);
      const saved = await accounting('basic-owner');
      assert.equal(saved.portfolio_import_batches.length, 1);
      assert.equal(saved.operations.length, 2);
      assert.equal(saved.portfolio_snapshots.length, 1);
      for (const table of ['operations', 'portfolio_snapshots']) {
        assert(saved[table].every(row => row.owner === 'basic-owner' && row.recorded_by === 'synthetic-qa'));
      }
      const prior = saved.portfolio_import_batches[0];
      assert.deepEqual(Object.keys(prior.backup).sort(), [...accountingTables].sort());
      assert(Object.values(prior.backup).every(items => Array.isArray(items) && items.length === 0));
      const replay = await imported('basic-owner', data);
      assert.deepEqual(replay, {...first, already_imported: true});
      assert.deepEqual(await accounting('basic-owner'), saved);
    });

    await check('changed workbook hash reuses source event identities without duplication', async () => {
      const before = await accounting('basic-owner', false);
      const result = await imported('basic-owner', bundle(2));
      assert.equal(result.inserted_operations, 0);
      assert.equal(result.skipped_operations, 2);
      assert.equal(result.inserted_positions, 0);
      assert.equal(result.skipped_positions, 1);
      assert.equal(result.pending_conflicts, 0);
      assert.equal(result.already_imported, false);
      assert.deepEqual(await accounting('basic-owner', false), before);
      assert.equal((await rows('portfolio_import_batches', 'basic-owner')).length, 2);
    });

    await check('owner arguments scope identities, writes, and private backups', async () => {
      const untouched = await accounting('unrelated-owner');
      const data = bundle(1);
      data.owner = 'unrelated-owner';
      // An unsupported top-level owner field fails closed, without mutation.
      // Row owner fields may exist in exported legacy records but cannot
      // override the trusted RPC owner used for every write and backup.
      await rejectsUnchanged('isolated-owner', data);
      delete data.owner;
      data.positions[0].owner = 'unrelated-owner';
      data.operations.forEach(row => { row.owner = 'unrelated-owner'; });
      const result = await imported('isolated-owner', data);
      assert.equal(result.inserted_operations, 2);
      assert.equal(result.inserted_positions, 1);
      assert.equal(result.already_imported, false);
      const saved = await accounting('isolated-owner');
      for (const table of ['operations', 'portfolio_snapshots', 'portfolio_import_batches']) {
        assert(saved[table].every(row => row.owner === 'isolated-owner'));
      }
      assert(Object.values(saved.portfolio_import_batches[0].backup).every(items => items.length === 0));
      assert.deepEqual(await accounting('unrelated-owner'), untouched);
    });

    await check('manual monetary conflicts are retained and backed up before additive inserts', async () => {
      const before = await accounting('manual-owner', false);
      const result = await imported('manual-owner', bundle(3, {
        positions: [position(), position({asset_name: 'New synthetic asset', analysis_ticker: 'NEW'})],
      }));
      assert.equal(result.pending_conflicts, 2);
      assert.equal(result.inserted_operations, 1);
      assert.equal(result.inserted_positions, 1);
      const saved = await accounting('manual-owner');
      assert.deepEqual(saved.portfolio_import_batches[0].backup, before);
      assert.equal(saved.portfolio_import_batches[0].metadata.pending_conflicts.length, 2);
      assert.deepEqual(saved.operations.find(row => row.id === before.operations[0].id), before.operations[0]);
      assert.deepEqual(saved.portfolio_snapshots.find(row => row.id === before.portfolio_snapshots[0].id), before.portfolio_snapshots[0]);
      assert.deepEqual(saved.portfolio_accounts, before.portfolio_accounts);
      assert.deepEqual(saved.private_investments, before.private_investments);
      assert(!JSON.stringify(saved.portfolio_import_batches[0].backup).includes('unrelated-owner'));
    });

    await check('identical manual event is reused without adding a source identity', async () => {
      await query(`INSERT INTO public.operations
        (owner,ticker,account_name,side,quantity,price,fees,settlement_amount_eur,fee_eur,
         fx_rate_to_eur,executed_at,notes,currency)
        VALUES ('exact-owner','AAA','Synthetic Broker','Compra',2,10,1,21,1,1,
         '2026-10-01T12:00:00Z','Synthetic manual note','EUR')`);
      const before = await rows('operations', 'exact-owner');
      const result = await imported('exact-owner', bundle(4, {operations: [operation()]}));
      assert.equal(result.skipped_operations, 1);
      assert.equal(result.inserted_operations, 0);
      assert.equal(result.pending_conflicts, 0);
      assert.deepEqual(await rows('operations', 'exact-owner'), before);
    });

    await check('same-day changed-price manual correction stays pending', async () => {
      const before = await rows('operations', 'exact-owner');
      const result = await imported('exact-owner', bundle(5, {
        operations: [operation({price: 11, source_event_id: 'synthetic-corrected'})],
      }));
      assert.equal(result.pending_conflicts, 1);
      assert.equal(result.inserted_operations, 0);
      assert.deepEqual(await rows('operations', 'exact-owner'), before);
    });

    await check('reused source identities cannot hide changed execution details', async () => {
      const before = await rows('operations', 'basic-owner');
      let number = 10;
      for (const change of [{quantity: 3}, {price: 11}, {account_name: 'Another synthetic broker'},
        {executed_at: '2026-10-02T12:00:00+00:00'}, {currency: 'USD'}, {fees: 4}]) {
        const result = await imported('basic-owner', bundle(number++, {operations: [operation(change)]}));
        assert.equal(result.pending_conflicts, 1);
        assert.equal(result.inserted_operations, 0);
        assert.equal(result.skipped_operations, 0);
        assert.deepEqual(await rows('operations', 'basic-owner'), before);
      }
    });

    await check('otherwise identical trades at separate brokers remain separate events', async () => {
      const result = await imported('broker-owner', bundle(20, {operations: [
        operation({account_name: 'Synthetic Broker A', source_event_id: 'broker-a'}),
        operation({account_name: 'Synthetic Broker B', source_event_id: 'broker-b'}),
      ]}));
      assert.equal(result.inserted_operations, 2);
      assert.equal(result.pending_conflicts, 0);
      assert.deepEqual((await rows('operations', 'broker-owner')).map(row => row.account_name).sort(),
        ['Synthetic Broker A', 'Synthetic Broker B']);
    });

    await check('native USD settlement and unknown fee or FX remain uninvented', async () => {
      const native = operation({ticker: 'USD', quantity: 10, price: 10, currency: 'USD', fees: 0,
        source_event_id: 'synthetic-usd', settlement_amount_local: 101,
        settlement_amount_eur: null, fee_eur: null, fx_rate_to_eur: null,
        notes: marker + JSON.stringify({source_event_id: 'synthetic-usd', fee_quality: 'unknown', fx_quality: 'reference'})});
      const euro = operation({ticker: 'EUR', fees: 0, source_event_id: 'synthetic-eur',
        settlement_amount_local: 21, settlement_amount_eur: 21, fee_eur: null,
        notes: marker + JSON.stringify({source_event_id: 'synthetic-eur', fee_quality: 'unknown', fx_quality: 'exact'})});
      const result = await imported('native-owner', bundle(21, {operations: [native, euro]}));
      assert.equal(result.inserted_operations, 2);
      const saved = await rows('operations', 'native-owner');
      const usd = saved.find(row => row.ticker === 'USD');
      assert.equal(Number(usd.settlement_amount_local), 101);
      assert.equal(usd.fees, 0);
      assert.equal(usd.fee_eur, null);
      assert.equal(usd.settlement_amount_eur, null);
      assert.equal(usd.fx_rate_to_eur, null);
      assert.equal(JSON.parse(usd.notes.slice(marker.length)).fee_quality, 'unknown');
      const eur = saved.find(row => row.ticker === 'EUR');
      assert.equal(eur.settlement_amount_eur, 21);
      assert.equal(eur.fee_eur, null);
    });

    await check('income metadata is preserved without synthesizing operations or capital flows', async () => {
      const data = bundle(22, {operations: []});
      data.metadata.events = [{event_type: 'dividend', amount_eur: 5, phase: 'historical'}];
      data.metadata.issues = [{code: 'synthetic-uncovered-flow'}];
      data.metadata.inventory = [{kind: 'synthetic-sheet', rows: 1}];
      const result = await imported('metadata-owner', data);
      assert.equal(result.inserted_operations, 0);
      assert.equal((await rows('operations', 'metadata-owner')).length, 0);
      const metadata = (await rows('portfolio_import_batches', 'metadata-owner'))[0].metadata;
      for (const field of ['events', 'issues', 'coverage', 'inventory', 'provenance']) {
        assert.deepEqual(metadata[field], data.metadata[field]);
      }
      assert.equal(metadata.coverage.capital_flows_complete, false);
    });

    await check('invalid bundles and duplicate payload identities reject without writes', async () => {
      const invalid = [null, [], {}, bundle(30, {source_sha256: 'invalid'}),
        bundle(30, {positions: []}), bundle(30, {positions: {}}),
        bundle(30, {operations: {}}), bundle(30, {metadata: []}),
        bundle(30, {positions: [null]}), bundle(30, {operations: [null]}),
        bundle(30, {positions: [position({snapshot_date: '2026-10-12'})]}),
        bundle(30, {positions: [position(), position()]}),
        bundle(30, {operations: [operation(), operation()]}),
        bundle(30, {operations: [operation({source_event_id: ''})]}),
        bundle(30, {operations: [operation({account_name: ''})]}),
        bundle(30, {operations: [operation({quantity: 0})]}),
        bundle(30, {operations: [operation({settlement_amount_local: -1})]}),
        bundle(30, {operations: [operation({quantity: 'not-a-number'})]}),
        bundle(30, {positions: [position({value_eur: -1})]}),
      ];
      for (const data of invalid) await rejectsUnchanged('invalid-owner', data);
      await rejectsUnchanged('', bundle(31));
      await rejectsUnchanged(null, bundle(31));
    });

    await check('late database failure rolls back backup, batch, and prior inserts', async () => {
      await db.exec('RESET ROLE');
      await db.exec(`CREATE FUNCTION public.synthetic_reconciled_failure() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.ticker='QA_FAIL' THEN RAISE EXCEPTION 'Synthetic failure'; END IF; RETURN NEW; END $$;
        CREATE TRIGGER synthetic_reconciled_failure BEFORE INSERT ON public.operations
        FOR EACH ROW EXECUTE FUNCTION public.synthetic_reconciled_failure();`);
      await db.exec('SET ROLE service_role');
      try {
        await rejectsUnchanged('rollback-owner', bundle(40, {operations: [operation(),
          operation({ticker: 'QA_FAIL', source_event_id: 'synthetic-failure'})]}));
      } finally {
        await db.exec('RESET ROLE');
        await db.exec('DROP TRIGGER synthetic_reconciled_failure ON public.operations; DROP FUNCTION public.synthetic_reconciled_failure();');
        await db.exec('SET ROLE service_role');
      }
    });

    await check('successful RPC participates in caller transaction rollback', async () => {
      const before = await accounting('transaction-owner');
      await db.exec('BEGIN');
      try {
        assert.equal((await imported('transaction-owner', bundle(41))).inserted_operations, 2);
      } finally { await db.exec('ROLLBACK'); }
      assert.deepEqual(await accounting('transaction-owner'), before);
    });

    await check('partial source identity index rejects duplicates only within the same owner', async () => {
      const statement = `INSERT INTO public.operations
        (owner,ticker,account_name,side,quantity,price,fees,executed_at,currency,source_event_id)
        VALUES ($1,'INDEX','Synthetic Index','Compra',1,1,0,'2026-01-01','EUR',$2)`;
      await query(statement, ['index-owner', 'synthetic-index']);
      await assert.rejects(() => query(statement, ['index-owner', 'synthetic-index']), error => error.code === '23505');
      await query(statement, ['other-index-owner', 'synthetic-index']);
      await query(statement, ['index-owner', null]);
      await query(statement, ['index-owner', null]);
      assert.equal((await rows('operations', 'index-owner')).length, 3);
    });

    await db.exec('RESET ROLE');
    await check('anon and authenticated cannot call RPC or read private backups', async () => {
      for (const role of ['anon', 'authenticated']) {
        await db.exec(`SET ROLE ${role}`);
        try {
          await assert.rejects(() => imported('basic-owner', bundle()), error => error.code === '42501');
          await assert.rejects(() => query('SELECT backup FROM public.portfolio_import_batches'), error => error.code === '42501');
          await assert.rejects(() => query("DELETE FROM public.portfolio_import_batches WHERE owner='basic-owner'"), error => error.code === '42501');
        } finally { await db.exec('RESET ROLE'); }
      }
    });

    await check('reapplying migration preserves all accounting and batch records', async () => {
      const before = {};
      for (const owner of ['manual-owner', 'basic-owner', 'unrelated-owner']) before[owner] = await accounting(owner);
      await db.exec(migration);
      for (const owner of Object.keys(before)) assert.deepEqual(await accounting(owner), before[owner]);
    });

    await check('fresh full schema and repeated deployment support the same RPC', async () => {
      const fresh = new PGlite();
      try {
        await fresh.waitReady;
        await fresh.exec(roles);
        await fresh.exec(schema);
        // Before schema.sql integration this also validates the migration on a
        // fresh deployment. Once embedded, reapplication must remain harmless.
        await fresh.exec(migration);
        await fresh.exec(schema);
        await fresh.exec(migration);
        await fresh.exec('SET ROLE service_role');
        const execute = async () => (await fresh.query('SELECT public.import_reconciled_portfolio($1,$2::jsonb) AS summary',
          ['fresh-owner', JSON.stringify(bundle(50))])).rows[0].summary;
        const first = await execute();
        assert.equal(first.inserted_operations, 2);
        assert.equal(first.inserted_positions, 1);
        assert.deepEqual(await execute(), {...first, already_imported: true});
      } finally { await fresh.close(); }
    });
    return passed;
  } finally { await db.close(); }
}

export default validateReconciledImport;

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const passed = await validateReconciledImport(process.argv[2]);
    process.stdout.write(`RESULT: ${passed} reconciled-import checks passed. Synthetic in-memory PostgreSQL only.\n`);
  } catch (error) {
    process.stderr.write(`FAILED: ${error.message}\n`);
    process.exitCode = 1;
  }
}
