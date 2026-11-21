# Market data quality gate — 2026-11-21

## Scope

No scores, investment thresholds, holdings, favorites, historical observations or
email recipients are changed. This release separates price transport quality from
fundamental/comparative coverage and prevents old data being promoted as current.
The Arena safety pause is persistent and is not automatically cleared by this release.

## Runtime contracts

- Each provider candidate is validated before selection. A rejected candidate
  allows the next provider to run; a successful HTTP response alone is not success.
- Daily Yahoo timestamps are mapped to exchange-local session dates using explicit
  exchange metadata. Symbol, granularity, OHLC, completed session, currency and
  quote units must agree. Unknown adjustments are not certified as adjusted.
- Radar has one saved-progress indicator. Price validation, full analysis,
  partial analysis and failed inputs have separate counts and per-symbol reasons.
- Entries require the exact saved snapshot, current completed close, matching
  price/currency, known units and an unblocked policy. Historical readings remain
  visible as historical, not executable entries. Missing data is N/D, not zero.
- Closing alerts use the same strict price downloader. Personal Arena coverage is
  bound to each report owner, independent of optional group email coverage.
- Arena requires a sealed price/analysis/coverage manifest. The SQL bridge and
  Worker reject partial, legacy or mismatched evidence; the sender omits an
  unverified report before obtaining mail credentials. A pause blocks planning,
  signals and state saves while retaining read access and saved history.
- Pause checks and state saves hold the same row lock. Saves cannot remove or
  forge the authoritative pause control and cannot replace state with a scalar.
- Corporate actions require reconciliation before an Arena liquidation; the
  implementation does not guess split-adjusted quantities or rewrite old rounds.

## Evidence and limits

| Check | Environment | Result / evidence |
| --- | --- | --- |
| Provider opening-time regression | Python, synthetic | Reproduced failure before fix; daily New York and Sydney bars now retain their local session. `test_provider_price_contract.py` |
| Old/failed data shown as current entries | Streamlit AppTest, synthetic | Regression cases exclude stale, partial, failed, corrupt and unverified-unit data. `test_entry_data_gate*.py` |
| One progress indicator and accurate completion text | Streamlit AppTest, synthetic | No inner download bar; price coverage and analysis completeness are distinct. `test_entry_data_gate_ui.py` |
| Mixed-version module compatibility | Python warm-import harness | Prior published app plus new modules remains importable; `test_radar_runtime_compat.py` |
| Multi-owner and group isolation | Python, synthetic | Both preference orders preserve personal manifests and intended email scope. `test_alert_runner.py` |
| Pause, grants, manifest and history preservation | PostgreSQL WASM, synthetic | `node scripts/validate_arena_pause.mjs` |
| Pause/write interleaving | PostgreSQL WASM, synthetic | Controlled interleaving and retained-lock checks passed; not a real multisesion load test. |
| Python report → SQL → Worker | Temporary offline harness, synthetic | Actual builder, SQL core and Worker preserve identities. SQL checks relational evidence; Python and Worker reject a modified manifest checksum. |
| Full Python suite | Local Python 3.13 | 2,304 tests passed; additional owner-isolation cases also passed independently. CI repeats against its Python 3.12 runtime. |
| Limited real provider check | Isolated downloader, no application DB/SMTP | AAPL, NVDA and IBE.MC retrieved the 2026-11-20 completed session with explicit currency from Yahoo direct after a metadata-poor candidate was rejected. |
| Browser visual/authenticated production journey | Not executed | Browser automation runtime unavailable. AppTest is not a real browser. An HTTP response or a login page does not validate authenticated navigation. |

The three real-provider samples do not independently verify the entire favorites
universe or every historical bar. Internal consistency checks are not independent
source corroboration. Providers may still fail and such failures must stay visible.

Ticker aliases whose saved spelling differs from the bridge's literal favorites
comparison remain conservatively blocked; this release does not silently rename
favorites or remove them from coverage. Arena must stay paused until the actual
required universe is reviewed and any corporate-action/history issues reconciled.

## Reproduction

```sh
python -m pytest -q
node scripts/validate_arena_pause.mjs .
```

The WASM test needs `@electric-sql/pglite`, as installed in CI. `PGLITE_MODULE`
can point to an existing local module. Synthetic tests do not send real mail,
alter production users, invoke investment models or execute real trades.
