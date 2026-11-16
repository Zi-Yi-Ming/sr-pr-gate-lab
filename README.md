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
