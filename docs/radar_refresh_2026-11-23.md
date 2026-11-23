# Radar refresh follow-up — 2026-11-23

## Reproduced defects

1. The ADX DataFrame join discarded price-frame attrs in pandas. A correctly
   normalized USD/EUR/GBp series then failed the downstream quote-unit check.
   Seven synthetic regressions failed before the fix. The indicator join now
   preserves a deep copy of the source metadata, including unknown/negative
   verification markers. Prices, scores and thresholds are unchanged.
2. A long-running full-page script retained the prior result as stale DOM.
   Merely clearing a placeholder, or changing a child container key, did not fix
   the repeated-update case: the real browser reproduced both old and new counter
   groups. The full page now finishes rendering a progress-only view before any
   provider I/O. Native fragment ticks process one durable batch at a time, with
   one stable progress slot. Saved result cards are not rendered during the job.
3. A batch had no visible per-provider phase. Six assets plus fundamentals,
   reference series and FX can take minutes. Phase callbacks now identify the
   symbol/stage in the single progress bar and renew the existing CAS lease at
   phase boundaries. This does not claim to interrupt a blocked network call.

## Preservation and behavior

- Navigation only reads a checkpoint; it does not consume update/resume flags.
- Work runs only after an explicit action. The final rerun rebuilds views from
  saved results once without repeating provider downloads.
- The initial fragment invocation defers I/O until the page has finished. Timer
  ticks run only the explicitly armed owner/run/view, one batch each. Leaving the
  view disarms it; reopening a page or browser requires explicit resume. Timer
  continuation cannot requeue completed incidents automatically.
- The refresh button is disabled while a start/resume is pending or another
  worker holds the claim. Confirmed batches survive interruptions.
- The old result surface and old warnings are not displayed as the new review;
  saved transport errors remain available in a clearly labeled details expander.
- A paused operation reports its last public phase, never provider bodies/tokens.
- The review context versions this corrected price pipeline. Older checkpoints
  are not retroactively certified; a fresh explicit review is required. Historical
  observations, favorites, holdings and email settings are not rewritten.
- An entrypoint hot-deploy guard reloads the indicator module before binding the
  new function. The offline prior-release test verifies metadata is recovered
  without clearing user state or cached journal resources.

## Validation

| Scope | Environment | Evidence |
| --- | --- | --- |
| Currency/provenance survives real preparation path | Synthetic Python | `tests/test_analysis_price_metadata.py`, USD/EUR/GBp and unverified currency |
| Provider delay beyond lease, takeover fencing, interruption and resume | Synthetic providers/time; real isolated SQLite | `tests/test_radar_refresh_liveness.py`; 175-symbol fixture resumes without repeating confirmed assets |
| One progress surface, public phase, heartbeat and read-only navigation | Synthetic Streamlit AppTest/Python | `tests/test_radar_refresh_surface.py` |
| Initial deferral, one batch per tick, explicit resume and no automatic incident retry | Synthetic Python/AppTest with isolated SQLite | `tests/test_radar_review_fragment.py` |
| Mixed prior/current deployment | Offline subprocess | `tests/test_radar_runtime_compat.py` |
| Full suite | Local Python | 2,326 tests passed; CI repeats on its own runtime before merge |
| Authenticated real browser, desktop 1920×993 | Local Chrome, QA account, simulated providers/analysis/cards; real main/navigation/checkpoints | Repeated update: one counter group, one progress bar, no saved card during work, no horizontal overflow |
| Interruption, new page session and resume | Same local Chrome plus SQLite audit | Paused with 156 confirmed/19 pending; navigating/reopening did not add provider calls; explicit resume reached 175 without repeating the 156 confirmed assets |
| Provider incident | Local browser/SQLite, synthetic | T007 stays `no_data`, `provider_unavailable`, not price-verified; final 174 valid/175 recorded, not 175 successes |
| Production | Separate, not certified by local QA | Public route reaches login in the isolated profile; authenticated production acceptance remains pending. No production refresh was triggered |
| Mobile | Not executed | Desktop results are not a mobile-browser certification |

The replay fixture is `tests/browser_radar_fixture.py`, bound to localhost, with
public test-only accounts and isolated SQLite. `SSL_RADAR_QA_DELAY` controls only
simulated latency. Example:

```sh
PYTHONPATH=. SSL_RADAR_QA_DELAY=0.2 python -m streamlit run tests/browser_radar_fixture.py --server.address 127.0.0.1 --server.port 18623 --server.headless true
```

Local screenshot evidence from this execution (not user data):

- `/private/tmp/ssl-radar-qa-repeat-bug-20261123.png` — stale second group reproduced before the fragment correction.
- `/private/tmp/ssl-radar-qa-repeat-fixed-20261123.png` — repeated review with one current group and no prior cards.
- `/private/tmp/ssl-radar-qa-fragment-working-20261123.png` — resumable progress-only view.

Arena remains paused. No test email, real-provider bulk refresh, real trade or
production user write was performed. The simulated timing tests prove state
safety, not real-provider latency. Local browser acceptance does not certify the
whole real favorites universe or authenticated production behavior.
