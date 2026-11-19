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

The calibration workflow uses manual dispatch. A `probe-only` dispatch can use an earlier calibration artifact without republishing checks. Runs, branches and PRs remain available for inspection.

## Native pull request control

The `pr-event-control.yml` workflow reports one native `PR Gate` for the pull request merge revision. It verifies the checkout SHA and both parents against the event, then checks `qualification.txt` for `ready` followed by a newline. Both cases use that same assertion.

| Head | Protected target | Fixture | Expected ordinary merge |
| --- | --- | --- | --- |
| `lab/pr-event-green` | `lab-target/pr-event-green` | `ready` | Succeeds |
| `lab/pr-event-red` | `lab-target/pr-event-red` | `broken` | Rejected for the failing `PR Gate` |

Create each target at the control workflow commit and its topic one fixture commit ahead. Before opening the PRs, require `PR Gate` from GitHub Actions (integration ID `15368`) on both targets and enforce the rule for administrators. Open each PR against its paired target to trigger the workflow. The workflow uses read-only credentials and leaves the merge decision to GitHub.

For each case, record the PR head, the test merge SHA and parents, the workflow event and conclusion, the native check ID and suite, and the required-check source. Verify the result with an ordinary protected merge pinned to the recorded head. The green case needs a successful merge and a target ref pointing at the resulting commit. The red case needs an explicit required-check rejection and an unchanged target. A permission error, conflict or head mismatch is inconclusive. This control establishes whether an eligible native job can satisfy branch protection on clean test branches.

## Cancelled suite coexistence

The coexistence control compares two new branch pairs at the same workflow base. Protect only the two target branches with `PR Gate` from GitHub Actions (integration ID `15368`) and no bypass actors.

| Head | Protected target | Current fixture | Required merge result |
| --- | --- | --- | --- |
| `lab/pr-event-stale-green` | `lab-target/pr-event-stale-green` | `ready` | Ordinary merge succeeds |
| `lab/pr-event-stale-red` | `lab-target/pr-event-stale-red` | `broken` | Ordinary merge rejected for the failing `PR Gate` |

1. Open each PR as a draft. The seed workflow checks `seed-qualification.txt` on the tested merge revision. After the native Gate succeeds, a separate job requests cancellation of its own run.
2. Before marking a PR ready, record the head and the seed run, suite and check IDs. Require a completed cancelled suite with a completed successful native `PR Gate` on that head. Stop if the seed Gate did not survive cancellation.
3. Mark the draft ready for review. Only that `pull_request` activity starts the current qualification workflow. It checks `qualification.txt` on the tested revision. Confirm an unchanged head and base, a new run and a distinct suite. Inspect `filter=all` to verify the old green check is still present and record `filter=latest` and GitHub's required-check state separately.
4. With both sets of evidence recorded, use one ordinary protected merge per case, pinned to that case's head. The red case needs an explicit `PR Gate` failure response, an open PR and an unchanged target. The green case needs a successful merge, the expected base and head as its parents, the tested file tree and the target pointing at the merge commit. A missing check, permission error, conflict or revision drift is inconclusive. An unexpected red merge invalidates the control.

The cancelled-suite Gate comes from a native job that checked the seed fixture. The control measures coexistence with the current native job. It does not recreate the production API publisher or establish a merge refusal while the PR is still a draft.

## API-published Gate and native Gate coexistence

Use fresh branch pairs `lab/pr-event-api-<case>` and `lab-target/pr-event-api-<case>` with the publisher branch `lab/pr-event-api-publisher` at the same frozen base commit. Require `PR Gate` from GitHub Actions App `15368` on the experimental targets, with no bypass actors.

| Case | Current authorization | Fixture | Current verdict | Expected ordinary merge |
| --- | --- | --- | --- | --- |
| `waiting` | Both expensive contracts deferred | `ready` | `awaiting-approval` | Rejected |
| `partial` | Only `expensive-a` approved | `ready` | `awaiting-approval` | Rejected |
| `failure` | Both expensive contracts approved | `broken` | `failure`, with process exit 1 | Rejected |
| `success` | Both expensive contracts approved | `ready` | `success` | Succeeds |

1. Open a draft for one branch pair. The seed workflow verifies the PR revision and cancels its own run. Record its completed, cancelled suite on the frozen head and the test merge's parents.
2. Add `lab-api-prior-qualification` while the PR is still a draft. This starts a distinct `pull_request` run. Its `CI Execution` job runs all three prior contracts and uploads the passing receipts for the same head, base, test merge and attempt. Keep the PR in draft until the API check is recorded.
3. Dispatch the registered `calibration.yml` workflow on `lab/pr-event-api-publisher` using `phase=publish-and-probe`, `source_run_id` set to the prior run ID, and `manifest` set to one JSON object with `case`, `pr`, `head`, `base`, `merge`, `seed_run`, `prior_run` and `prior_attempt`. PR, run and attempt fields are positive integers. On this branch only the coexistence publisher is eligible. It verifies the draft, the old qualification receipt and both suites before sending one `POST /check-runs` with `semantic-router-ci:<pr>:<run>:<attempt>`. GitHub chooses the suite. Record the returned ID and read back `all` and `latest`. If GitHub assigns another suite, the intended topology was not established. If the POST outcome is uncertain, check the head for the existing record before any further action.
4. Once the original green Gate is verified in the cancelled suite, mark the draft ready. The current workflow binds its native `PR Gate` to the tested revision and emits the actual receipt. Record both Gate IDs, their suites and requiredness, and the current fixture result. Use the same head and base throughout. The waiting and partial cases log `awaiting-approval` and exit nonzero in this control.
5. Evaluate only cases with the intended topology and exact revision evidence. For the positive case, an ordinary protected merge must succeed and the target must point to a merge commit with the tested tree and expected parents. Each negative case needs an explicit required-Gate refusal from GitHub, an open unmerged PR and an unchanged target. A permission error, missing evidence or revision drift is inconclusive. The workflows leave all merge requests to the operator.

This control uses the same head-bound Checks API publication and run-attempt identity as the production publisher. The trusted controller dispatch stands in for the production `workflow_run` trigger. It uses synthetic grants and contracts and measures GitHub's merge decision. The production authorization handler, pending presentation, fork trust boundary and Mergify are outside this control.

Run local tests with:

```text
python -m unittest discover -s tests -v
```
