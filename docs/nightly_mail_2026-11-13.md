# Nightly delivery: independent stages and late closing prices

Prepared on a review branch. This change does not publish the app, modify any
portfolio or destination, spend model credits, release delivery reservations,
or resend an already attempted message.

## What failed

A nightly job can accept the Stock Signal Lab summaries at SMTP and fail
afterwards while preparing the independent Arena message. Previously both
stages ran inside one script and every Arena exception had the same generic
warning. A red job therefore did not identify which message was outstanding.

There is a separate data-coverage issue: a quote within two business days is
usable for some technical checks, but is not evidence for the target closing
session. A report correctly leaves that session's score missing. This must not
be interpreted as zero opportunities, a failed fundamental thesis, or a sell.

## Changes

- Separate named workflow steps for Stock Signal Lab and Arena. The overall
  workflow still fails if Arena fails; it is not made green by hiding errors.
- The first step supplies its target market session to Arena. A late second
  step does not silently select a new date; an unsafe skipped first step
  explicitly prevents Arena from running.
- Arena errors have a safe stage, a bounded reason code and HTTP status.
  Remote bodies, addresses, tokens, headers and raw exceptions stay out of
  public CI output. HTTP 409 is not labelled as an exhausted balance.
- A scheduled current-session review retries only lagging quote series, once
  per symbol and at most 180 symbols. It uses the existing downloader and
  actual exchange calendar. An absent new close remains absent.
- Quote metadata survives indicator calculation. Different currency units are
  rejected, entire series replace entire series, and future rows do not enter
  the requested report. Manual/default reviews do not acquire extra requests.
- Existing SMTP claims and Arena reservations remain intact. A reserved or
  uncertain attempt is not automatically recycled. Accepted messages are not
  resent to recover a later failure.

No investment rules, scores, thresholds, moving averages, RSI, MACD, stops,
real operations or email preferences are changed.

## Validation and limits

The automated tests use synthetic owners, quotes, HTTP responses and SMTP
receipts. They cover staged HTTP failures, invalid JSON, network timeouts,
deduplication, uncertain SMTP, confirmed SMTP followed by failed confirmation,
late/unchanged/future closes, holidays and currency conflicts.

Production investigation was read-only. It checked workflow/job logs, the
affected owner's report/delivery metadata, Arena runtime error status and
saved planning state. A limited public-provider check confirmed that a more
recent close was subsequently available. No production workflow was rerun,
and no real test mail was sent.

Free provider availability is not guaranteed by this change. A bounded
retry cannot fabricate a missing close or repair historical reports in place.
An HTTP 409 without a retained response body does not establish which
application precondition failed; the new diagnostics distinguish future
failures without exposing the body.

After review and approval, publish the branch normally and inspect both
workflow steps separately. Do not retry the complete old job merely to
recover Arena: first inspect its existing delivery reservation and current
session. If SMTP acceptance is uncertain, reconcile it before any new send.
