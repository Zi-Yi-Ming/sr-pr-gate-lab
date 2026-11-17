# PR Gate lab

Isolated GitHub platform experiments for semantic-router issue [#5661](https://github.com/vllm-project/semantic-router/issues/5661).

All writes are restricted to `Zi-Yi-Ming/sr-pr-gate-lab`. This repository contains synthetic fixtures, with no production source checkout, credentials, models or GPU workloads.

## Basic calibration

Four separate PR heads exercise waiting for authorization, partial authorization, a real failed test fixture, and full qualification. The failure fixture runs a fixed Python subprocess that exits nonzero; its output and exit status are retained. Authorization partitions are synthetic and do not test the production `/ci run` permission handler.

The workflow evaluates fixtures, publishes actual required `PR Gate` checks through the GitHub Actions App, then attempts ordinary merges. The publisher has checks write permission and read-only contents. A separate probe job has contents write permission but no checks write or administration permission. It attempts the negative controls first, and only then the successful PR. No personal token is stored in Actions.

Protection requires `PR Gate` from GitHub Actions and applies to administrators. Each case uses an independent head; all cases share a frozen base until the successful PR is merged last. There is no merge queue, required review, status spoofing, branch update, admin bypass, or automatic comment.

The basic calibration deliberately isolates the required-check and merge contract. Its native workflow jobs execute on the trusted main revision, while the API checks are attached to the individual test PR heads. It does not reproduce the multi-suite defect. Native head-bound suites, cancelled-suite topology and recovery routes A/B are subsequent experiments, gated on completed passing calibration evidence.

## Evidence and continuation

Artifacts `basic-qualification`, `basic-publication` and `basic-probe` preserve inputs, check identities and merge responses. Incomplete readback is inconclusive. A permission error, conflict or head mismatch is not a passing negative control. A negative PR merging is unsafe and stops the probe.

The only trigger is manual dispatch. There is no watcher, scheduled job, polling loop or sleep. A `probe-only` dispatch can use an earlier calibration artifact without republishing checks. Runs, branches and PRs remain available for inspection.

Run local tests with:

```text
python -m unittest discover -s tests -v
```

## A/B multi-suite experiment

The basic calibration passed on run [43895533473](https://github.com/Zi-Yi-Ming/sr-pr-gate-lab/actions/runs/43895533473): waiting and partial authorization returned explicit required-Gate HTTP405 refusals; the failed fixture returned a failing-Gate refusal; the full-success PR merged normally. Its raw artifacts remain the basic evidence.

The A/B controller lives on `lab/ab-controller`. Default main retains the basic workflow. Each new A/B case has its own independently protected target branch with the same required check/App/admin contract and its own frozen head. No basic PR is changed.

The fixed case order is A waiting/partial/failure/success, then B waiting/partial/failure/success. A seed run on the case head dispatches qualification and cancels itself. A separate run on the same head executes qualification and the native `CI Execution` aggregate. Its relay starts the driver on the immutable controller revision. One concurrency slot makes these finite stages sequential, without waiting loops or timers.

The driver checks actual run/suite provenance before publishing. Negative cases deliberately include an explicitly synthetic stale-success fixture; it is not qualification evidence. The current Gate must still remain pending or failed, and actual ordinary merge calls must be refused. Positive cases must first reproduce the required-green/cancelled-suite/merge-refused topology; absence of that topology is `NOT_REPRODUCED`.

Route A PATCHes only the exact current Gate. Route B POSTs one replacement and retains old checks. Publisher and recovery jobs can write checks but cannot merge; baseline and final probes can merge but cannot rewrite checks or administer the repository. Native and foreign checks are never neutralized.

An unsafe negative merge, permission error or inconclusive result stops the finite batch. A well-defined terminal result can advance exactly one position. `NOT_REPRODUCED`, `NOT_RECOVERED` and inconsistent merge/readback evidence are never presented as a successful recovery. There are no repeated dispatch retries, polling, scheduled monitors or comments.

## A merge-decision supplement

The first three A negative fixtures passed actual Gate-specific HTTP405 probes. The strict A success baseline stopped before a merge attempt because `Commit.statusCheckRollup` was null. Direct account and PR-context GraphQL reads also returned null; this does not establish a permission problem or a platform root cause. A third, empty queued suite belongs to Cursor and is left untouched.

`lab/ab-supplement-a` preserves the original controller, case heads and artifacts. Its three additional jobs perform a bounded merge-decision experiment against only the original A success fixture. The original publication from run `44048669391` is pinned by its canonical JSON SHA256; original qualification, source run/attempt, check identities, current requiredness and frozen head/base are revalidated. The source producer is never rewritten to impersonate the supplemental driver.

A baseline ordinary merge must explicitly return HTTP405 naming `PR Gate`, with the PR still unmerged, before the recovery job can PATCH the exact original successful Gate. Other required contexts, conflicts, permission errors, missing evidence, changed checks and changed qualification stop the supplement. There is no Gate POST, suite update, admin bypass, rerun, B dispatch or comment. Check-writing and merge permissions remain separate.

The final ordinary merge response, PR readback, merge parents and target ref are retained. `MERGED_PROBE_ONLY` and `STILL_BLOCKED_PROBE_ONLY` describe only the measured merge decision; `strict_recovery_verified` is always false. If the baseline already merges, the result is `BASELINE_MERGED_NO_RECOVERY_NEEDED` and no PATCH occurs. None of these outcomes substitutes for the original strict topology or production acceptance.
