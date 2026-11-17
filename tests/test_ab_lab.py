"""Finite A/B experiment contracts; no network or credentials are used."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ab_lab as ab
import lab

REPO = "Zi-Yi-Ming/sr-pr-gate-lab"
P = "/repos/" + REPO
BASE = "525bc8117c9176b158d206b324c904e18aab8534"
CONTROLLER = "f" * 40
FIXTURES = ("waiting", "partial", "failure", "success")
CASES = [{"case_id": route + "_" + fixture, "route": route, "fixture": fixture,
          "pr": 10 + i * 4 + j, "head": format(1 + i * 4 + j, "040x"), "base": BASE,
          "base_ref": "lab-target/" + route.lower() + "-" + fixture,
          "head_ref": "lab/ab-" + route.lower() + "-" + fixture}
         for i, route in enumerate(("A", "B")) for j, fixture in enumerate(FIXTURES)]


def envelope(index=0, stage="driver"):
    value = {"schema_version": 1, "controller_ref": "lab/ab-controller", "controller_sha": CONTROLLER,
             "cases": copy.deepcopy(CASES), "index": index, "stage": stage}
    if stage != "seed": value["seed_run_id"] = "100"
    if stage == "driver": value["qualification_run_id"] = "200"
    return value


def environment(envelope):
    stage = envelope["stage"]
    case = envelope["cases"][envelope["index"]]
    ref = envelope["controller_ref"] if stage == "driver" else case["head_ref"]
    return {"GITHUB_REPOSITORY": REPO, "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_RUN_ID": {"seed": "100", "qualification": "200", "driver": "300"}[stage],
            "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": CONTROLLER if stage == "driver" else case["head"],
            "GITHUB_REF": "refs/heads/" + ref, "LAB_AB_MANIFEST": json.dumps(envelope)}


def qualification(index=0):
    env = envelope(index, "qualification")
    return ab.execute("qualify", env, env=environment(env))


class FakeAPI:
    def __init__(self, index=0):
        self.case = copy.deepcopy(CASES[index]); self.calls = []; self.merged = False
        self.required = True; self.post_merge_required = None; self.merge_result = None; self.latest_missing = False
        self.null_rollup = False; self.post_merge_null_rollup = False
        self.mutate_foreign = False; self.bad_readback = False
        self.base_sha = BASE; self.advance_base_after_post = False
        self.merge_sha = "9" * 40; self.merge_parents = [BASE, self.case["head"]]
        self.runs = {}
        for run_id, suite, conclusion in ((100, 501, "cancelled"),
                (200, 502, "failure" if self.case["fixture"] == "failure" else "success")):
            self.runs[run_id] = {"id": run_id, "path": ".github/workflows/calibration.yml", "event": "workflow_dispatch",
                "head_branch": self.case["head_ref"], "head_sha": self.case["head"], "run_attempt": 1,
                "status": "completed", "conclusion": conclusion, "check_suite_id": suite,
                "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO}}
        self.suites = {suite: {"id": suite, "head_sha": self.case["head"], "app": {"id": 15368},
                              "status": "completed", "conclusion": conclusion}
                       for suite, conclusion in ((501, "cancelled"), (502, self.runs[200]["conclusion"]), (503, "success"))}
        self.checks = {}
        for i, suite in ((11, 501), (12, 502)):
            self.checks[i] = self.make_check(i, suite, "CI Execution", "completed", self.suites[suite]["conclusion"], None)
        self.jobs = [{"id": 21, "name": "CI Execution", "status": "completed", "conclusion": self.runs[200]["conclusion"]},
                     {"id": 22, "name": "Native qualification", "status": "completed", "conclusion": self.runs[200]["conclusion"]},
                     {"id": 23, "name": "Relay", "status": "completed", "conclusion": "success"}]

    def make_check(self, check_id, suite, name, status, conclusion, external):
        return {"id": check_id, "node_id": "NODE_" + str(check_id), "name": name,
                "head_sha": self.case["head"], "app": {"id": 15368}, "check_suite": {"id": suite},
                "status": status, "conclusion": conclusion, "external_id": external}

    def pr(self):
        return {"number": self.case["pr"], "draft": False, "state": "closed" if self.merged else "open",
                "merged": self.merged, "merged_at": "2026-11-17T00:00:00Z" if self.merged else None,
                "head": {"sha": "b" * 40 if self.bad_readback and self.merged else self.case["head"],
                         "ref": self.case["head_ref"], "repo": {"full_name": REPO}},
                "base": {"sha": "e" * 40 if self.merged else BASE, "ref": self.case["base_ref"], "repo": {"full_name": REPO}}}

    def request(self, method, path, data=None):
        self.calls.append((method, path, copy.deepcopy(data)))
        if path == "/graphql":
            node = data["variables"]["node"]
            authoritative = self.checks[int(node.split("_")[1])]
            rollup = "SUCCESS" if authoritative["check_suite"]["id"] == 503 else "FAILURE"
            rollup = None if self.null_rollup or (self.merged and self.post_merge_null_rollup) else {"state": rollup}
            required = self.post_merge_required if self.merged and self.post_merge_required is not None else self.required
            return 200, {"data": {"check": {"id": node, "isRequired": required},
                                  "repository": {"pullRequest": {"headRefOid": self.case["head"]},
                                  "object": {"oid": self.case["head"], "statusCheckRollup": rollup}}}}
        if path == P + "/actions/workflows/calibration.yml/dispatches" and method == "POST": return 204, {}
        if path == P + "/actions/runs/100/cancel" and method == "POST": return 202, {"message": "accepted"}
        if path == P + "/pulls/" + str(self.case["pr"]) and method == "GET": return 200, self.pr()
        if path == P + "/git/ref/heads/" + self.case["base_ref"] and method == "GET":
            return 200, {"ref": "refs/heads/" + self.case["base_ref"],
                         "object": {"type": "commit", "sha": self.merge_sha if self.merged else self.base_sha}}
        if path == P + "/git/commits/" + self.merge_sha and method == "GET":
            return 200, {"sha": self.merge_sha, "parents": [{"sha": sha} for sha in self.merge_parents]}
        if path == P + "/pulls/" + str(self.case["pr"]) + "/merge" and method == "PUT":
            if self.merge_result is not None:
                status, body = self.merge_result
            elif self.case["fixture"] == "success" and any(c["name"] == "PR Gate" and c["check_suite"]["id"] == 503 for c in self.checks.values()):
                status, body = 200, {"merged": True}
            else: status, body = 405, {"message": "Required status check PR Gate is expected."}
            if body.get("merged") is True:
                self.merged = True
                body = dict(body, sha=self.merge_sha)
            return status, copy.deepcopy(body)
        if method == "GET":
            if path.startswith(P + "/actions/workflows/calibration.yml/runs?"):
                return 200, {"total_count": len(self.runs), "workflow_runs": copy.deepcopy(list(self.runs.values()))}
            if path == P + "/actions/runs/200/jobs?per_page=100":
                return 200, {"total_count": len(self.jobs), "jobs": copy.deepcopy(self.jobs)}
            for run_id, run in self.runs.items():
                if path == P + "/actions/runs/" + str(run_id): return 200, copy.deepcopy(run)
            for suite_id, suite in self.suites.items():
                if path == P + "/check-suites/" + str(suite_id): return 200, copy.deepcopy(suite)
            for check_id, check in self.checks.items():
                if path == P + "/check-runs/" + str(check_id): return 200, copy.deepcopy(check)
            if path.startswith(P + "/commits/" + self.case["head"] + "/check-runs?"):
                checks = list(self.checks.values())
                if "filter=latest" in path:
                    gate = [c for c in checks if c["name"] == "PR Gate"]
                    checks = [c for c in checks if c["name"] != "PR Gate"] + ([] if self.latest_missing else gate[-1:])
                return 200, {"total_count": len(checks), "check_runs": copy.deepcopy(checks)}
        if method == "POST" and path == P + "/check-runs":
            already = any(c.get("external_id") == data["external_id"] for c in self.checks.values())
            check_id = 1000 + len(self.checks)
            check = self.make_check(check_id, 503 if already else 501, data["name"], data["status"], data.get("conclusion"), data["external_id"])
            self.checks[check_id] = check
            if self.mutate_foreign: self.checks[11]["conclusion"] = "neutral"
            if self.advance_base_after_post: self.base_sha = "e" * 40
            return 201, copy.deepcopy(check)
        if method == "PATCH" and path.startswith(P + "/check-runs/"):
            check = self.checks[int(path.rsplit("/", 1)[1])]
            check.update({k: v for k, v in data.items() if k != "output"})
            if self.mutate_foreign: self.checks[11]["conclusion"] = "neutral"
            return 200, copy.deepcopy(check)
        raise AssertionError("Unexpected request " + method + " " + path)


def run(command, index=0, api=None, document=None):
    stage = "seed" if command == "seed" else "qualification" if command in ("qualify", "relay") else "driver"
    e = envelope(index, stage)
    return ab.execute(command, e, client=api, document=document, env=environment(e))


def published(index=0):
    api = FakeAPI(index)
    q = qualification(index)
    p = run("publish", index, api, q)
    return api, p


class ManifestTests(unittest.TestCase):
    def test_exact_eight_cases_refs_bases_and_no_extra_keys(self):
        self.assertEqual(ab.validate_envelope(envelope())["index"], 0)
        mutants = []
        for key, value in (("index", True), ("index", 8), ("stage", "anything"), ("controller_ref", "main"), ("extra", True)):
            e = envelope(); e[key] = value; mutants.append(e)
        for key, value in (("case_id", "B_waiting"), ("pr", True), ("head_ref", "main"), ("base_ref", "main"), ("base", "a" * 40), ("extra", 1)):
            e = envelope(); e["cases"][0][key] = value; mutants.append(e)
        e = envelope(); e["cases"][1]["head"] = e["cases"][0]["head"]; mutants.append(e)
        e = envelope(); e["cases"][1]["pr"] = e["cases"][0]["pr"]; mutants.append(e)
        e = envelope(); e["cases"] = e["cases"][::-1]; mutants.append(e)
        e = envelope(); e["cases"] = e["cases"][:7]; mutants.append(e)
        e = envelope(); e["qualification_run_id"] = "100"; mutants.append(e)
        for e in mutants:
            with self.subTest(e=e), self.assertRaises(lab.LabError): ab.validate_envelope(e)

    def test_context_is_head_for_seed_qualification_controller_for_driver_and_attempt_one(self):
        for command in ("seed", "qualify", "publish"):
            stage = {"seed": "seed", "qualify": "qualification", "publish": "driver"}[command]
            e = envelope(stage=stage)
            for key, value in (("GITHUB_REPOSITORY", "other/repo"), ("GITHUB_SHA", "e" * 40),
                               ("GITHUB_REF", "refs/heads/main"), ("GITHUB_RUN_ATTEMPT", "2"), ("GITHUB_EVENT_NAME", "push")):
                api = FakeAPI()
                result = ab.execute(command, e, api, env=dict(environment(e), **{key: value}))
                self.assertEqual(result["outcome"], "INCONCLUSIVE")
                self.assertEqual(api.calls, [])


class QualificationAndSeedTests(unittest.TestCase):
    def test_real_failure_receipt_and_immutable_deferred_inventory(self):
        results = [qualification(i) for i in range(4)]
        self.assertEqual([r["gate_status"] for r in results], ["awaiting-approval", "awaiting-approval", "failure", "success"])
        self.assertEqual([len(r["deferred"]) for r in results], [2, 1, 0, 0])
        self.assertEqual(results[2]["contracts"][0]["execution"]["returncode"], 1)
        self.assertIn("synthetic failure", results[2]["contracts"][0]["execution"]["stderr"])
        for result in results:
            self.assertEqual(result["required_contracts"], ["automatic", "expensive-a", "expensive-b"])
            self.assertEqual(result["qualification_run_id"], "200")

    def test_failure_cli_exits_one_after_saving_nested_receipt(self):
        e = envelope(2, "qualification")
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, environment(e), clear=True):
            output = Path(d) / "out" / "qualification.json"
            self.assertEqual(ab.main(["qualify", "--output", str(output)]), 1)
            self.assertEqual(json.loads(output.read_text())["gate_status"], "failure")

    def test_seed_dispatches_qualification_then_self_cancel_without_assuming_completion(self):
        api = FakeAPI(); result = run("seed", api=api)
        self.assertTrue(result["all_passed"], result)
        writes = [c for c in api.calls if c[0] != "GET"]
        self.assertEqual([c[1] for c in writes], [P + "/actions/workflows/calibration.yml/dispatches", P + "/actions/runs/100/cancel"])
        dispatch = writes[0][2]
        self.assertEqual(dispatch["ref"], CASES[0]["head_ref"])
        self.assertEqual(dispatch["inputs"]["phase"], "publish-and-probe")
        next_e = json.loads(dispatch["inputs"]["manifest"])
        self.assertEqual(next_e["stage"], "qualification"); self.assertEqual(next_e["seed_run_id"], "100")
        self.assertNotIn("qualification_run_id", next_e)
        self.assertEqual(dispatch["inputs"]["source_run_id"], "")

    def test_seed_contamination_prevents_any_write(self):
        api = FakeAPI(); api.checks[99] = api.make_check(99, 501, "PR Gate", "completed", "success", "lab-ab:stale:A_waiting:99")
        result = run("seed", api=api)
        self.assertFalse(result["all_passed"])
        self.assertFalse(any(c[0] != "GET" for c in api.calls))

    def test_relay_dispatches_controller_and_binds_actual_qualification_id(self):
        api = FakeAPI(2); result = run("relay", 2, api)
        self.assertTrue(result["all_passed"], result)
        body = api.calls[0][2]
        self.assertEqual(body["ref"], "lab/ab-controller")
        e = json.loads(body["inputs"]["manifest"])
        self.assertEqual(e["stage"], "driver"); self.assertEqual(e["qualification_run_id"], "200")


class PublicationTests(unittest.TestCase):
    def test_negatives_create_explicit_stale_green_then_actual_derived_current(self):
        for index, conclusion in ((0, None), (1, None), (2, "failure"), (3, "success")):
            with self.subTest(index=index):
                api, result = published(index)
                self.assertTrue(result["all_passed"], result)
                posts = [c[2] for c in api.calls if c[0] == "POST"]
                self.assertEqual(len(posts), 1 if index == 3 else 2)
                if index != 3:
                    self.assertEqual(posts[0]["external_id"], "lab-ab:stale:" + CASES[index]["case_id"] + ":100")
                    self.assertEqual(posts[0]["conclusion"], "success")
                    self.assertIn("synthetic stale", posts[0]["output"]["summary"])
                self.assertEqual(posts[-1].get("conclusion"), conclusion)
                self.assertEqual(posts[-1]["external_id"], "lab-ab:current:" + CASES[index]["case_id"] + ":200:1")

    def test_unfinished_mismatched_cancelled_seed_and_invalid_native_result_fail_before_posts(self):
        mutations = [lambda a: a.runs[100].update(status="in_progress", conclusion=None),
                     lambda a: a.runs[100].update(conclusion="success"),
                     lambda a: a.runs[200].update(head_sha="b" * 40),
                     lambda a: a.runs[200].update(repository={"full_name": "other/repo"}),
                     lambda a: a.runs[200].update(run_attempt=2),
                     lambda a: a.runs[200].update(path=".github/workflows/other.yml"),
                     lambda a: a.suites[501].update(app={"id": 999}),
                     lambda a: a.jobs[0].update(conclusion="failure"),
                     lambda a: a.runs.update({201: dict(a.runs[200], id=201)})]
        q = qualification()
        for mutate in mutations:
            api = FakeAPI(); mutate(api)
            with self.subTest(mutate=mutate):
                result = run("publish", api=api, document=q)
                self.assertFalse(result["all_passed"])
                self.assertFalse(any(c[0] == "POST" for c in api.calls))

    def test_forged_failure_receipt_cannot_be_upgraded_and_partial_posts_are_retained(self):
        q = qualification(2); q["gate_status"] = "success"
        api = FakeAPI(2)
        self.assertFalse(run("publish", 2, api, q)["all_passed"])
        self.assertEqual(api.calls, [])
        api, publication = published()
        calls = len(api.calls)
        result = run("publish", api=api, document=qualification())
        self.assertFalse(result["all_passed"])
        self.assertFalse(any(c[0] == "POST" for c in api.calls[calls:]))

    def test_real_target_ref_overrides_stale_pr_base_and_is_checked_before_each_post(self):
        api = FakeAPI(); api.base_sha = "e" * 40
        result = run("publish", api=api, document=qualification())
        self.assertFalse(result["all_passed"])
        self.assertFalse(any(c[0] == "POST" for c in api.calls))
        api = FakeAPI(); api.advance_base_after_post = True
        result = run("publish", api=api, document=qualification())
        self.assertFalse(result["all_passed"])
        self.assertIsNotNone(result["stale_check"]); self.assertIsNone(result["initial_check"])
        self.assertEqual(len([c for c in api.calls if c[0] == "POST"]), 1)


class RecoveryTests(unittest.TestCase):
    def test_null_positive_rollup_stops_before_merge_with_evidence_reason(self):
        api, publication = published(3); api.null_rollup = True; api.calls.clear()
        result = run("baseline", 3, api, publication)
        self.assertEqual(result["outcome"], "INCONCLUSIVE")
        self.assertFalse(result["eligible"]); self.assertFalse(result["advance"])
        self.assertFalse(result["all_passed"])
        self.assertIn("statusCheckRollup", result["error"])
        self.assertIsNone(result["evidence"]["before"]["graphql"]["body"]["data"]["repository"]["object"]["statusCheckRollup"])
        self.assertFalse(any(c[0] == "PUT" for c in api.calls))

    def test_negative_baseline_refusal_is_eligible_but_generic_or_permission_is_not(self):
        for index in (0, 1, 2, 4, 5, 6):
            api, publication = published(index)
            baseline = run("baseline", index, api, publication)
            self.assertTrue(baseline["eligible"], baseline)
            self.assertFalse(baseline["advance"])
        for code, message in ((403, "PR Gate"), (409, "head changed"), (405, "Nope"),
                (405, 'Required status check "Security" is expected.'),
                (405, 'Required status check "PR Gateway" is expected.'),
                (405, 'Required status check "PR Gate extra" is expected.')):
            api, publication = published(); api.merge_result = code, {"message": message}
            result = run("baseline", api=api, document=publication)
            self.assertFalse(result["eligible"]); self.assertFalse(result["advance"])
            self.assertEqual(result["outcome"], "INCONCLUSIVE")

    def test_positive_topology_absent_or_merge_success_is_not_reproduced(self):
        api, publication = published(3); api.suites[501]["conclusion"] = "success"
        result = run("baseline", 3, api, publication)
        self.assertEqual(result["outcome"], "NOT_REPRODUCED"); self.assertTrue(result["advance"])
        self.assertFalse(any(c[0] == "PUT" for c in api.calls))
        api, publication = published(3); api.merge_result = 200, {"merged": True}
        result = run("baseline", 3, api, publication)
        self.assertEqual(result["outcome"], "NOT_REPRODUCED"); self.assertTrue(result["advance"])

    def test_A_only_patches_exact_current_B_posts_and_retains_all_old_checks(self):
        for index in (0, 1, 2, 3, 4, 5, 6, 7):
            with self.subTest(index=index):
                api, publication = published(index)
                baseline = run("baseline", index, api, publication)
                self.assertTrue(baseline["eligible"], baseline)
                before = copy.deepcopy(api.checks); api.calls.clear()
                result = run("recover", index, api, baseline)
                self.assertTrue(result["all_passed"], result)
                writes = [c for c in api.calls if c[0] != "GET" and c[1] != "/graphql"]
                self.assertEqual(len(writes), 1)
                if index < 4:
                    self.assertEqual(writes[0][0], "PATCH")
                    self.assertTrue(writes[0][1].endswith("/" + str(publication["initial_check"]["id"])))
                    self.assertNotIn("head_sha", writes[0][2]); self.assertNotIn("external_id", writes[0][2])
                else:
                    self.assertEqual(writes[0][0], "POST")
                    self.assertEqual(writes[0][2]["external_id"], publication["initial_check"]["external_id"])
                    self.assertEqual(len(api.checks), len(before) + 1)
                for check_id, check in before.items():
                    if check_id != publication["initial_check"]["id"] or index >= 4:
                        self.assertEqual(api.checks[check_id], check)

    def test_recover_refuses_forged_eligibility_and_detects_foreign_mutation(self):
        api, publication = published()
        forged = {"eligible": True, "advance": True}
        self.assertFalse(run("recover", api=api, document=forged)["all_passed"])
        baseline = run("baseline", api=api, document=publication)
        api.mutate_foreign = True
        result = run("recover", api=api, document=baseline)
        self.assertEqual(result["outcome"], "INCONCLUSIVE")

    def test_negative_merge_success_is_unsafe_and_not_advanced(self):
        api, publication = published(); api.merge_result = 200, {"merged": True}
        baseline = run("baseline", api=api, document=publication)
        self.assertEqual(baseline["outcome"], "UNSAFE"); self.assertFalse(baseline["advance"])

    def test_baseline_and_recovery_require_live_frozen_target_ref(self):
        api, publication = published()
        api.base_sha = "e" * 40; api.calls.clear()
        result = run("baseline", api=api, document=publication)
        self.assertFalse(result["all_passed"])
        self.assertFalse(any(c[0] == "PUT" for c in api.calls))
        api, publication = published()
        baseline = run("baseline", api=api, document=publication)
        api.base_sha = "e" * 40; api.calls.clear()
        result = run("recover", api=api, document=baseline)
        self.assertFalse(result["all_passed"])
        self.assertFalse(any(c[0] in ("PATCH", "PUT") or (c[0] == "POST" and c[1] != "/graphql") for c in api.calls))


class ProbeAndAdvanceTests(unittest.TestCase):
    def recovered(self, index):
        api, publication = published(index)
        baseline = run("baseline", index, api, publication)
        recovery = run("recover", index, api, baseline)
        self.assertTrue(recovery["all_passed"], recovery)
        return api, recovery

    def test_pending_partial_failure_stay_negative_for_both_routes(self):
        for index in (0, 1, 2, 4, 5, 6):
            api, recovery = self.recovered(index)
            result = run("probe", index, api, recovery)
            self.assertEqual(result["outcome"], "PASS", result); self.assertTrue(result["advance"])
            merge = [c for c in api.calls if c[0] == "PUT"][-1]
            self.assertEqual(merge[2], {"sha": CASES[index]["head"], "merge_method": "merge"})

    def test_positive_recovered_not_recovered_and_inconsistent_readback_are_distinct(self):
        api, recovery = self.recovered(3)
        a = run("probe", 3, api, recovery)
        self.assertEqual(a["outcome"], "NOT_RECOVERED"); self.assertTrue(a["advance"])
        api, recovery = self.recovered(7)
        b = run("probe", 7, api, recovery)
        self.assertEqual(b["outcome"], "RECOVERED", b); self.assertTrue(b["advance"])
        api, recovery = self.recovered(7); api.bad_readback = True
        inconsistent = run("probe", 7, api, recovery)
        self.assertEqual(inconsistent["outcome"], "MERGED_INCONSISTENT_READBACK"); self.assertTrue(inconsistent["advance"])

    def test_required_before_merge_may_be_false_after_valid_success(self):
        api, recovery = self.recovered(7); api.post_merge_required = False
        result = run("probe", 7, api, recovery)
        self.assertEqual(result["outcome"], "RECOVERED", result)
        self.assertTrue(result["evidence"]["before"]["graphql"]["body"]["data"]["check"]["isRequired"])
        self.assertFalse(result["evidence"]["after"]["graphql"]["body"]["data"]["check"]["isRequired"])
        self.assertTrue(run("advance", 7, api, result)["batch_complete"])

    def test_null_postmerge_rollup_preserves_merge_evidence_without_claiming_recovery(self):
        api, recovery = self.recovered(7); api.post_merge_null_rollup = True
        result = run("probe", 7, api, recovery)
        self.assertEqual(result["outcome"], "MERGED_INCONSISTENT_READBACK", result.get("error"))
        self.assertTrue(result["evidence"]["merge"]["body"]["merged"])
        self.assertIn("statusCheckRollup", result["evidence"]["readback_mismatch"])
        self.assertTrue(run("advance", 7, api, result)["batch_complete"])

    def test_not_required_before_merge_blocks_probe(self):
        api, recovery = self.recovered(7); api.required = False; api.calls.clear()
        result = run("probe", 7, api, recovery)
        self.assertEqual(result["outcome"], "INCONCLUSIVE")
        self.assertFalse(result["advance"])
        self.assertFalse(any(c[0] == "PUT" for c in api.calls))

    def test_advance_rechecks_requiredness_in_premerge_probe_evidence(self):
        api, recovery = self.recovered(0)
        result = run("probe", api=api, document=recovery)
        self.assertEqual(result["outcome"], "PASS")
        result["evidence"]["before"]["graphql"]["body"]["data"]["check"]["isRequired"] = False
        api.calls.clear()
        advanced = run("advance", api=api, document=result)
        self.assertEqual(advanced["outcome"], "INCONCLUSIVE")
        self.assertFalse(advanced["all_passed"])
        self.assertEqual(api.calls, [])

    def test_missing_latest_or_changed_old_check_blocks_merge(self):
        for change in ("latest", "old"):
            api, recovery = self.recovered(4); api.calls.clear()
            if change == "latest": api.latest_missing = True
            else: api.checks[11]["conclusion"] = "neutral"
            result = run("probe", 4, api, recovery)
            self.assertEqual(result["outcome"], "INCONCLUSIVE"); self.assertFalse(result["advance"])
            self.assertFalse(any(c[0] == "PUT" for c in api.calls))

    def test_probe_checks_live_base_and_positive_merge_commit_parents(self):
        api, recovery = self.recovered(4); api.base_sha = "e" * 40; api.calls.clear()
        result = run("probe", 4, api, recovery)
        self.assertEqual(result["outcome"], "INCONCLUSIVE")
        self.assertFalse(any(c[0] == "PUT" for c in api.calls))
        api, recovery = self.recovered(7); api.merge_parents = ["e" * 40, CASES[7]["head"]]
        result = run("probe", 7, api, recovery)
        self.assertEqual(result["outcome"], "MERGED_INCONSISTENT_READBACK")
        self.assertTrue(result["advance"])

    def test_permission_errors_preserve_status_body_and_stop_advance(self):
        api, recovery = self.recovered(0); api.merge_result = 403, {"message": "Forbidden"}
        result = run("probe", api=api, document=recovery)
        self.assertEqual(result["outcome"], "INCONCLUSIVE"); self.assertFalse(result["advance"])
        self.assertEqual(result["permission_error"], {"status": 403, "body": {"message": "Forbidden"}})

    def test_advance_finite_chain_clears_ids_and_refuses_forged_flags(self):
        api, recovery = self.recovered(0)
        probe = run("probe", api=api, document=recovery)
        api.calls.clear()
        result = run("advance", api=api, document=probe)
        self.assertTrue(result["all_passed"], result)
        dispatch = api.calls[-1][2]
        next_e = json.loads(dispatch["inputs"]["manifest"])
        self.assertEqual(next_e["index"], 1); self.assertEqual(next_e["stage"], "seed")
        self.assertNotIn("seed_run_id", next_e); self.assertNotIn("qualification_run_id", next_e)
        self.assertEqual(dispatch["ref"], CASES[1]["head_ref"])
        api.calls.clear()
        self.assertFalse(run("advance", api=api, document={"advance": True, "outcome": "PASS"})["all_passed"])
        self.assertEqual(api.calls, [])
        api, recovery = self.recovered(7)
        probe = run("probe", 7, api, recovery); api.calls.clear()
        result = run("advance", 7, api, probe)
        self.assertTrue(result["batch_complete"]); self.assertEqual(api.calls, [])


class TransportTests(unittest.TestCase):
    def test_operation_allowlist_cannot_write_wrong_repo_comment_admin_or_foreign_check(self):
        e = envelope()
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network prohibited")):
            api = ab.API("recover", e, dict(environment(e), GH_TOKEN="dummy"))
            for method, path, data in (("POST", P + "/issues/1/comments", {}),
                    ("PUT", P + "/branches/main/protection", {}), ("PATCH", P + "/check-runs/11", {"status": "completed"}),
                    ("POST", "/repos/other/repo/check-runs", {}), ("POST", P + "/actions/runs/300/cancel", {}),
                    ("GET", P + "/../other", None)):
                with self.subTest(path=path), self.assertRaises(lab.LabError): api.request(method, path, data)


if __name__ == "__main__":
    unittest.main()
