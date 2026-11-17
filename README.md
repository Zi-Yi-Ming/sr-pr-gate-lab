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

## Versioned B continuation

The A supplement completed on run [44053499983](https://github.com/Zi-Yi-Ming/sr-pr-gate-lab/actions/runs/44053499983). Its exact PATCH returned HTTP200, but ordinary merge returned exact PR Gate HTTP405 both before and after. The Gate remained in the cancelled seed suite; the PR remained unmerged and other checks were preserved. This is `STILL_BLOCKED_PROBE_ONLY`, not strict recovery evidence.

`lab/ab-b2-controller` runs only the four B cases, starting at index4, on new `lab/ab-b2-*` heads and independent `lab-target/b2-*` protected targets. Earlier A/B refs, PRs and evidence remain frozen. The first three cases retain the pending/partial/real-failure negative controls, including synthetic stale-success checks. The success case explicitly measures only merge behavior with complete qualification, a required successful Gate in a cancelled seed suite, and an exact Gate-specific HTTP405 baseline before the one replacement POST.

Current native run/attempt, qualification suite and jobs are checked again before the replacement and final merge. Only the cancelled seed suite's aggregate may change after mutation; this does not relax seed-run cancellation provenance or current qualification. The actual replacement ID and owning suite are recorded; no successful association is assumed. All/latest/required checks and preservation of the original Gate and other checks remain mandatory. Positive outcomes are always limited merge-decision outcomes, even if rollup becomes readable. They cannot be upgraded to `RECOVERED`. No extra permissions, CI polling, retries or comments are added.

## Native PR Gate experiment

`lab/native-controller` adds an independent four-case experiment (`native-pr-gate-v1`) without changing the older jobs, refs, fixtures or evidence. Each case has a new `lab/native-<fixture>` head and protected `lab-target/native-<fixture>` target, frozen at base `525bc8117c9176b158d206b324c904e18aab8534`. The finite order is waiting, partial, failure, success. Only manual dispatch and the existing shared concurrency slot are used.

The seed cancels itself after dispatching qualification on the same head. Qualification executes the three fixed Python contracts and preserves actual subprocess outputs, exit codes and deferred authorization partitions. New aggregate names are `Native experiment qualification` and `Native experiment CI Execution`, distinct from the preserved legacy jobs. A controller run publishes the explicitly marked stale-success negative fixture and current API Gate, then attempts an ordinary merge. Only an exact `PR Gate` HTTP405 plus an actually unmerged PR permits the native stage. If the last success baseline already merges with verified parents and target ref, the batch ends without a native dispatch.

The head-bound job is named `PR Gate` only on `native-gate`; on every other stage its inactive name cannot create a skipped required Gate. It always runs on its native stage. Missing or invalid source evidence fails the job. Waiting, partial and real-failure qualifications produce a valid negative verdict artifact and exit 1; this is a lab fail-closed representation, not production waiting UX. Only complete current successful qualification may exit 0. No native job can POST or PATCH a CheckRun.

The relay validates the completed Gate while its own run can still be in progress. The controller probe then requires the fully completed third head-bound run, actual suite conclusion and successful relay. It binds the authoritative CheckRun through the exact `PR Gate` job's `check_run_url`, never by job ID equality, a name alone or `external_id`. Old checks must remain unchanged; additions must map to actual jobs in the native suite. A queued, waiting or skipped non-Gate job may lack a URL, but cannot authorize any added CheckRun. Any unexpected failure or unmapped addition stops the batch.

All reports retain real producers and the original source chain. Cross-run artifact downloads are pinned by run ID; the native manifest additionally pins the canonical baseline JSON SHA256. Current requiredness, latest visibility, source attempts, frozen head/base and check preservation are revalidated before mutation. Check-write, merge and dispatch permissions are separate. Permission errors, generic denials, other required contexts and missing evidence are inconclusive. A negative PR merging is unsafe. Positive outcomes are only `MERGED_NATIVE_PROBE_ONLY`, `STILL_BLOCKED_NATIVE_PROBE_ONLY`, or `BASELINE_MERGED_NO_RECOVERY_NEEDED`; null rollup observations remain null, and `strict_recovery_verified` always remains false.

`native_lab.py` takes `NATIVE_MANIFEST` from the workflow's JSON `inputs.manifest`. Every command accepts `--output`; consumer commands also accept `--input`, and native relay/probe accept `--native-input`. The workflow uses these fixed artifacts:

| Command | Input artifact(s) | Output artifact / file |
| --- | --- | --- |
| `seed` | none | `native-seed/seed.json` |
| `qualify` | none | `native-qualification/qualification.json` |
| `qual-relay` | qualification, same run | `native-qual-relay/qual-relay.json` |
| `publish` | qualification, exact source run | `native-publication/publication.json` |
| `baseline` | publication, same run | `native-baseline/baseline.json` |
| `native-dispatch` | baseline, same run | `native-dispatch/native-dispatch.json` |
| `gate` | pinned baseline | `native-verdict/native.json` |
| `native-relay` | pinned baseline; same-run verdict | `native-relay/native-relay.json` |
| `probe` | pinned baseline; pinned native verdict | `native-probe/probe.json` |
| `advance` | same-run terminal probe or last-case baseline | `native-advance/advance.json` |

Reports expose `protocol_valid`, `gate_success`, `eligible`, `advance` and `batch_complete` via `GITHUB_OUTPUT`. An invalid protocol exits 1, as do deliberate negative `gate` verdicts and the actual failed `qualify` fixture. Reports and failure artifacts are retained with `always()` upload steps. Local tests use real fixed subprocesses and controlled HTTP responses; they prove guards and state transitions, not GitHub's actual merge decision. No platform recovery is claimed from local tests.
