# Degree-routing release

This release fixes the seven known routing failures in the October 30, 2026
audit and adds an offline test gate before scheduled posting. Employer pages,
public ATS responses and legacy description lookup use the same classifier.
There are no company-specific degree exceptions.

Required versus preferred qualifications, separate mandatory conditions,
alternative applicant pathways, equivalent experience and inline spacing are
covered by regression tests. Old parser caches are invalidated through a new
namespace. Interrupted employer/source downloads retry, and failed employer
reads stay visible as `fetch_failed`. Failed first-run/backfill delivery does
not silently mark selected, previously unseen jobs as sent.

## Acceptance checks

Run from the repository root, using Python 3.9+:

```bash
python -m unittest -v
python scripts/replay_routing_audit.py
```

For the coverage gate, install the development-only dependency:

```bash
python -m pip install 'coverage>=7,<8'
python -m coverage run --branch --source=job_bot,employer_degree -m unittest -v
python -m coverage report --fail-under=90
```

The runtime and regular tests require only the standard library. CI tests
Python 3.9, 3.11 and 3.13. The posting workflow pins Python 3.11 and refuses
to post or commit state if the preceding test step fails.

The frozen audit retains all 120 sampled listings and their original reference
labels, endpoint provenance and response hashes. One ambiguous reference is
excluded from scoring; a recorded download failure remains a failure in replay.
The 119 clear references include 30 graduate-required and 89 other roles.
Every clear case must route correctly for the suite to pass.

This is now a development benchmark. Its labels came from a single assistant
source review without independent human adjudication. Passing all 119 cases
does not establish 100% production accuracy. Measure future accuracy on a new,
independently reviewed sample, with both false graduate exclusions and missed
graduate requirements reported.

## Deployment

1. Merge the reviewed release into `main` after the Tests workflow passes.
   Fork PR workflows may require approval from a repository maintainer.
   Keep the current production `seen_jobs.json` and `degree_cache.json`.
   Do not reset the bot's delivery history.
2. In GitHub Actions, run **ACM Job Radar** with `dry_run: true` and the
   backfill inputs blank. Check `[employer]` lookup statuses and `[route]`
   counts, including graduate and unverified results. This sends no Discord
   messages and does not save state with the default `DRY_RUN_SAVE=0`.
3. Confirm the internships and new-grad webhook secrets exist. Configure the
   optional graduate webhook to receive graduate-only roles; otherwise those
   roles are intentionally skipped. The graduate channel never falls back to
   an undergraduate webhook.
4. Run the normal workflow with the backfill inputs blank. Check the test
   step, posting result, state-save push and messages in the intended channels.
   Live Discord acceptance is a separate check from the automated suite.

Defaults verify up to 40 fresh employer URLs per run, matching the normal
40-post cap. Backfills can exceed that budget and retain fallback decisions
for remaining jobs. A page can also be blocked, expire or provide no parseable
evidence. Such cases stay explicitly unverified when feed/title evidence is
also absent. Unknown evidence is not proof that a bachelor's student qualifies.

## Recovery and rollback

After a delivery exception, confirmed successful batches are saved in the
`finally` block. Previously unseen, unsent jobs remain eligible for subsequent
normal runs while within the freshness window. If they have aged out, select
an appropriate date window for a deliberate backfill. Re-running a backfill
also re-posts jobs already delivered in that window.

State persistence depends on the runner completing the save and pushing it.
An abrupt kill or a lost Discord acknowledgement can still create duplicates;
this release does not promise exactly-once delivery.

For a code rollback, revert the release commit while preserving the latest
delivery state. Run tests and a dry run before resuming posting. A rollback
restores the earlier parser's known routing errors. Existing Discord posts
are not moved or removed by either release or rollback.
