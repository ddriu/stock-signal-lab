# Saved-close report bridge — local validation

Base: `d70c230a17e19fe9ad251dc33d6dbcb5ae8b768f`.
Branch: `codex/arena-report-bridge-20261124`. Not deployed.

## Behavior

- A confirmed final Radar batch archives a report from the owner's saved
  observations. Opening a page or reading progress does not archive anything.
- A failed archive does not undo a finished review. The separate retry reads
  saved observations only; it does not repeat market-provider calls.
- Delayed scheduled runs can recover this archive outside the email window.
  That path is explicitly owner-scoped and cannot run mail or Arena.
- The target is the latest **completed XNYS session**, not the local date or
  an intraday quote. Foreign-market history keeps its own session labels.
- ISINs remain in favorites but are not guessed to be US tickers or assigned
  an XNYS calendar. Missing genuine US inputs still block authorization.
- Reports retain immutable identifiers. Archive-only reports identify that
  purpose explicitly; they do not claim email delivery, simulation execution,
  or the lifting of an Arena pause. Blocked reports show their missing inputs.

## Tests and evidence

All accounts, observations and provider results in the tests below are synthetic.

Full local Python suite: **2,396 passed**. The remaining warnings are existing
NumPy/pandas deprecations. A final focused CLI/Arena run also passed **81 tests**
after removing private per-owner coverage counts from public Actions output.
`git diff --check` passed.

| Environment | Check | Result / evidence |
|---|---|---|
| Python + SQLite | Final batch archives; incomplete, cancelled and unconfirmed batches do not | `tests/test_radar_report_bridge.py` |
| Python + SQLite | Archive failure preserves checkpoint; retry does not repeat market work | Same test file |
| Python + SQLite | Immutable archive, idempotence, unchanged prior report and ledgers | `tests/test_saved_close_report.py` |
| Python | Closed session, midnight, holidays, early closes, foreign sessions, partial/empty data | Same test file |
| Python | Owner isolation, unknown owner and nullable/mixed owner rejection, bounded reads for 175 favorites | Same test file |
| Python | Late schedule archives without providers, email, or Arena | `tests/test_saved_close_report_cli.py`, `tests/test_arena_digest.py` |
| Python subprocess | Upgrade from actual earlier source with loaded modules; repeat boot has no reload loop | `tests/test_isin_runtime_compat.py`, existing warm-deploy regressions |
| Real Chrome, logged-in local QA | Native Radar queue completes 175 fictitious symbols; actual journal archives one blocked report | `tests/browser_radar_fixture.py`; isolated SQLite evidence |
| Real Chrome, same QA | Explicit report retry: 350 simulated provider-stage calls before and after; one report before and after | `qa_provider_calls`, `daily_score_reports` in isolated SQLite |
| Real Chrome, QA | One report-status group, no remaining progress bar, no horizontal overflow; no provider resources in page | DOM inspection after completion |
| Real Chrome, QA | Archived link displays archive-only and Arena-blocked notices | `/private/tmp/ssl-arena-report-blocked-20261124.png` |
| Real Chrome, second QA account | First account's report link is unavailable; no substitute report is displayed | Authenticated second-session page inspection |

Browser providers, calculations and authentication are simulated. The Radar
main, native navigation, batch/checkpoint path, SQLite archive, report renderer
and retry are real application code. The intentionally incomplete synthetic
report must remain blocked; this does not validate live quotes.

## Explicitly not verified here

- No deployment or authenticated production cycle with this patch.
- No production report writes, actual email, quote-provider validation, model
  calls, Arena activation, historical-round repair, or real/virtual operations.
- No new mobile-browser or full navigation-map certification in this change.
- The earlier independent navigation/disarm issue is not part of this patch.

Publishing must not be described as making Arena operational. Its data gate,
current inputs, explicit pause and execution-window checks remain authoritative.
