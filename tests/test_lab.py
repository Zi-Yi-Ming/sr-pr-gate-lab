"""Offline regression tests: every HTTP interaction uses a deterministic fake."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lab


REPO = "Zi-Yi-Ming/sr-pr-gate-lab"
PREFIX = "/repos/" + REPO
MANIFEST = [
    {"case_id": name, "pr": i, "head": str(i) * 40, "base": "a" * 40}
    for i, name in enumerate(("waiting", "partial", "failure", "success"), 1)
]
ENV = {"LAB_MANIFEST": json.dumps(MANIFEST), "GITHUB_REPOSITORY": REPO,
       "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}


def qualification():
    return lab.qualify(ENV)


def pr(case, merged=False):
    return {"number": case["pr"], "state": "closed" if merged else "open",
            "draft": False, "merged": merged, "merged_at": "2026-11-16T00:00:00Z" if merged else None,
            "head": {"sha": case["head"]},
            "base": {"sha": case["base"], "ref": "main", "repo": {"full_name": REPO}}}


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.checks = {}
        self.merged = set()
        self.merge_results = {}
        self.required = True
        self.rollup = "SUCCESS"
        self.latest_override = None
        self.receipt_override = None
        self.pr_override = None
        self.suite_override = None
        self.force_merged_after = False

    def request(self, method, path, data=None):
        self.calls.append((method, path, copy.deepcopy(data)))
        if path == "/graphql":
            return 200, {"data": {"check": {"id": data["variables"]["node"], "isRequired": self.required},
                                  "repository": {"pullRequest": {"headRefOid": data["variables"]["head"]},
                                      "object": {"oid": data["variables"]["head"],
                                                 "statusCheckRollup": {"state": self.rollup}}}}}
        if path == PREFIX + "/check-runs" and method == "POST":
            check = dict(data, id=100 + len(self.checks), node_id="CHECK_" + str(len(self.checks)),
                         app={"id": 15368}, check_suite={"id": 777})
            check.setdefault("conclusion", None)
            if self.receipt_override:
                check.update(self.receipt_override)
            self.checks[check["head_sha"]] = check
            return 201, copy.deepcopy(check)
        for case in MANIFEST:
            pull_path = PREFIX + "/pulls/" + str(case["pr"])
            if path == pull_path and method == "GET":
                body = pr(case, case["pr"] in self.merged)
                if body["merged"]:
                    body["base"]["sha"] = "e" * 40
                if self.pr_override:
                    body.update(self.pr_override)
                return 200, body
            if path == pull_path + "/merge" and method == "PUT":
                code, body = self.merge_results.get(case["case_id"],
                    (200, {"merged": True}) if case["case_id"] == "success" else
                    (405, {"message": "Required status check PR Gate is expected."}))
                if body.get("merged") is True:
                    self.merged.add(case["pr"])
                if self.force_merged_after:
                    self.merged.add(case["pr"])
                return code, body
            for filter_name in ("all", "latest"):
                if path == PREFIX + "/commits/" + case["head"] + "/check-runs?filter=" + filter_name + "&per_page=100":
                    found = [self.checks[case["head"]]] if case["head"] in self.checks else []
                    if filter_name == "latest" and self.latest_override is not None:
                        found = self.latest_override
                    return 200, {"total_count": len(found), "check_runs": copy.deepcopy(found)}
        for check in self.checks.values():
            if path == PREFIX + "/check-runs/" + str(check["id"]):
                return 200, copy.deepcopy(check)
        if path == PREFIX + "/check-suites/777":
            body = {"id": 777, "status": "completed", "conclusion": "success",
                    "head_sha": MANIFEST[3]["head"], "app": {"id": 15368}}
            body.update(self.suite_override or {})
            return 200, body
        raise AssertionError("Unexpected API call: " + method + " " + path)


class QualificationTests(unittest.TestCase):
    def test_fixed_process_failure_and_deferred_contracts_drive_status(self):
        doc = qualification()
        self.assertEqual([q["gate_status"] for q in doc["qualifications"]],
                         ["awaiting-approval", "awaiting-approval", "failure", "success"])
        self.assertEqual([len(q["deferred"]) for q in doc["qualifications"]], [2, 1, 0, 0])
        for q in doc["qualifications"]:
            self.assertEqual(q["required_contracts"], ["automatic", "expensive-a", "expensive-b"])
            self.assertEqual([c["id"] for c in q["contracts"]], ["automatic", "expensive-a", "expensive-b"])
        failing = doc["qualifications"][2]["contracts"][0]["execution"]
        self.assertEqual(failing["returncode"], 1)
        self.assertIn("synthetic failure", failing["stderr"])
        self.assertIn("LAB synthetic", failing["stdout"])

    def test_manifest_rejects_duplicate_order_extra_and_wrong_types(self):
        invalid = []
        for key, value in (("pr", True), ("pr", 0), ("head", "x" * 40), ("base", "z" * 40)):
            m = copy.deepcopy(MANIFEST); m[0][key] = value; invalid.append(m)
        m = copy.deepcopy(MANIFEST); m[1]["pr"] = 1; invalid.append(m)
        m = copy.deepcopy(MANIFEST); m[1]["head"] = m[0]["head"]; invalid.append(m)
        m = copy.deepcopy(MANIFEST); m[1]["base"] = "b" * 40; invalid.append(m)
        m = copy.deepcopy(MANIFEST); m[0]["extra"] = "ignored?"; invalid.append(m)
        invalid += [MANIFEST[::-1], MANIFEST[:3], MANIFEST + [MANIFEST[0]], {}]
        for manifest in invalid:
            with self.subTest(manifest=manifest), self.assertRaises(lab.LabError):
                lab.qualify(dict(ENV, LAB_MANIFEST=json.dumps(manifest)))

    def test_run_identity_and_repository_are_strict(self):
        for key, value in (("GITHUB_RUN_ID", "0"), ("GITHUB_RUN_ATTEMPT", "-1"),
                           ("GITHUB_REPOSITORY", "other/repo")):
            with self.subTest(key=key), self.assertRaises(lab.LabError):
                lab.qualify(dict(ENV, **{key: value}))

    def test_duplicate_json_case_keys_are_not_silently_overwritten(self):
        raw = json.dumps(MANIFEST).replace('"case_id": "waiting"', '"case_id": "ignored", "case_id": "waiting"')
        with self.assertRaises(lab.LabError):
            lab.qualify(dict(ENV, LAB_MANIFEST=raw))


class PublishTests(unittest.TestCase):
    def test_publishes_bound_states_and_complete_receipts(self):
        api = FakeAPI(); report = lab.publish(qualification(), api)
        self.assertTrue(report["all_passed"], report)
        posts = [data for method, _, data in api.calls if method == "POST"]
        self.assertEqual([p["status"] for p in posts], ["in_progress", "in_progress", "completed", "completed"])
        self.assertNotIn("conclusion", posts[0]); self.assertNotIn("conclusion", posts[1])
        self.assertEqual([p.get("conclusion") for p in posts[2:]], ["failure", "success"])
        self.assertEqual(posts[0]["external_id"], "sr-pr-gate-lab:basic:1:123:1")
        self.assertEqual(posts[0]["head_sha"], "1" * 40)
        self.assertEqual(len(report["receipts"]), 4)

    def test_existing_id_refuses_duplicate_and_preserves_partial_report(self):
        doc = qualification(); api = FakeAPI()
        existing = lab.publish(doc, api)
        self.assertTrue(existing["all_passed"])
        count = len(api.calls)
        report = lab.publish(doc, api)
        self.assertFalse(report["all_passed"])
        self.assertIn("probe-only", report["error"])
        self.assertFalse(any(c[0] == "POST" for c in api.calls[count:]))

    def test_forged_success_and_receipt_mismatch_fail_closed(self):
        for mutate in (lambda d: d["qualifications"][2].update(gate_status="success"),
                       lambda d: d["qualifications"][0].update(deferred=[]),
                       lambda d: d.update(repository="other/repo")):
            doc = qualification(); mutate(doc); api = FakeAPI()
            self.assertFalse(lab.publish(doc, api)["all_passed"])
            self.assertEqual(api.calls, [])
        api = FakeAPI(); api.receipt_override = {"app": {"id": 999}}
        report = lab.publish(qualification(), api)
        self.assertFalse(report["all_passed"])
        self.assertEqual(len(report["receipts"]), 1)

    def test_pr_base_mismatch_blocks_write(self):
        api = FakeAPI(); api.pr_override = {"base": {"sha": "b" * 40, "ref": "main", "repo": {"full_name": REPO}}}
        self.assertFalse(lab.publish(qualification(), api)["all_passed"])
        self.assertFalse(any(c[0] == "POST" for c in api.calls))

    def test_publish_matches_source_run_attempt_and_workflow_sha(self):
        env = dict(ENV, GITHUB_SHA="f" * 40)
        doc = lab.qualify(env)
        self.assertEqual(doc["source_workflow_sha"], "f" * 40)
        for key, value in (("GITHUB_SHA", "e" * 40), ("GITHUB_RUN_ID", "999"), ("GITHUB_RUN_ATTEMPT", "2")):
            api = FakeAPI()
            report = lab.publish(doc, api, dict(env, **{key: value}))
            self.assertFalse(report["all_passed"])
            self.assertEqual(api.calls, [])
        self.assertTrue(lab.publish(doc, FakeAPI(), env)["all_passed"])

    def test_unexpected_transport_failure_still_returns_partial_report(self):
        api = FakeAPI()
        original = api.request
        def breaking(method, path, data=None):
            if path == PREFIX + "/pulls/2":
                raise RuntimeError("synthetic transport failure")
            return original(method, path, data)
        api.request = breaking
        report = lab.publish(qualification(), api)
        self.assertFalse(report["all_passed"])
        self.assertEqual(len(report["receipts"]), 1)

    def test_publish_preserves_permission_status_and_body(self):
        api = FakeAPI()
        api.request = lambda *args: (403, {"message": "Forbidden"})
        report = lab.publish(qualification(), api)
        self.assertFalse(report["all_passed"])
        self.assertEqual(report["permission_error"], {"status": 403, "body": {"message": "Forbidden"}})


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.publication = lab.publish(qualification(), self.api)
        self.assertTrue(self.publication["all_passed"])
        self.api.calls.clear()

    def test_negative_first_success_last_and_ordinary_merge_head_binding(self):
        report = lab.probe(self.publication, self.api)
        self.assertTrue(report["all_passed"], report)
        merges = [(path, data) for method, path, data in self.api.calls if method == "PUT"]
        self.assertEqual([path for path, _ in merges], [PREFIX + "/pulls/" + str(i) + "/merge" for i in range(1, 5)])
        self.assertEqual([data for _, data in merges], [{"sha": str(i) * 40, "merge_method": "merge"} for i in range(1, 5)])
        self.assertEqual([c["outcome"] for c in report["cases"]], ["PASS", "PASS", "PASS", "PASS"])

    def test_permission_conflict_and_generic_refusal_are_never_negative_passes(self):
        for code, message in ((403, "Required status check PR Gate"), (409, "Head changed"), (405, "Not allowed")):
            with self.subTest(code=code):
                self.api.merge_results["waiting"] = (code, {"message": message})
                report = lab.probe(self.publication, self.api)
                self.assertFalse(report["all_passed"])
                self.assertNotEqual(report["cases"][0]["outcome"], "PASS")
                self.assertFalse(any(path.endswith("/4/merge") for method, path, _ in self.api.calls if method == "PUT"))

    def test_unexpected_negative_merge_stops_unsafe(self):
        self.api.merge_results["waiting"] = (200, {"merged": True})
        report = lab.probe(self.publication, self.api)
        self.assertFalse(report["all_passed"])
        self.assertEqual(report["cases"][0]["outcome"], "UNSAFE")
        self.assertEqual(len([c for c in self.api.calls if c[0] == "PUT"]), 1)

    def test_negative_postread_detects_unsafe_even_if_merge_response_claims_refusal(self):
        self.api.force_merged_after = True
        report = lab.probe(self.publication, self.api)
        self.assertFalse(report["all_passed"])
        self.assertEqual(report["cases"][0]["outcome"], "UNSAFE")

    def test_latest_visibility_required_and_exact_receipt_are_enforced(self):
        for change in ("missing", "not_required", "changed", "old_receipt"):
            with self.subTest(change=change):
                api = FakeAPI(); publication = lab.publish(qualification(), api); api.calls.clear()
                if change == "missing": api.latest_override = []
                if change == "not_required": api.required = False
                if change == "changed": api.checks["1" * 40]["status"] = "completed"
                if change == "old_receipt": publication["receipts"][0]["check"]["external_id"] = "foreign"
                report = lab.probe(publication, api)
                self.assertFalse(report["all_passed"])
                self.assertFalse(any(c[0] == "PUT" for c in api.calls))

    def test_failed_gate_cannot_be_upgraded_in_artifact(self):
        self.publication["receipts"][2]["check"]["conclusion"] = "success"
        report = lab.probe(self.publication, self.api)
        self.assertFalse(report["all_passed"])
        self.assertFalse(any(c[0] == "PUT" for c in self.api.calls))

    def test_success_requires_suite_rollup_and_merged_confirmation(self):
        for change in ("rollup", "suite", "not_merged"):
            with self.subTest(change=change):
                api = FakeAPI(); publication = lab.publish(qualification(), api); api.calls.clear()
                if change == "rollup": api.rollup = "PENDING"
                if change == "suite": api.suite_override = {"conclusion": "failure"}
                if change == "not_merged": api.merge_results["success"] = (200, {"merged": False})
                report = lab.probe(publication, api)
                self.assertFalse(report["all_passed"])
                self.assertEqual(report["cases"][-1]["outcome"], "INCONCLUSIVE")


class CLITests(unittest.TestCase):
    def test_qualify_creates_missing_nested_output_directory(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV, clear=True):
            output = Path(directory) / "out" / "nested" / "qualification.json"
            self.assertFalse(output.parent.exists())
            exit_code = lab.main(["qualify", "--output", str(output)])
            self.assertEqual(exit_code, 0)
            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(document["kind"], "qualification")
            self.assertEqual([q["gate_status"] for q in document["qualifications"]],
                             ["awaiting-approval", "awaiting-approval", "failure", "success"])


class HTTPTests(unittest.TestCase):
    def test_wrong_repo_and_nonallowlisted_paths_never_reach_transport(self):
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network prohibited")):
            with self.assertRaises(lab.LabError):
                lab.GitHubAPI({"GITHUB_REPOSITORY": "other/repo", "GH_TOKEN": "dummy"})
            api = lab.GitHubAPI({"GITHUB_REPOSITORY": REPO, "GH_TOKEN": "dummy"})
            for method, path in (("POST", PREFIX + "/issues/1/comments"),
                                 ("PATCH", PREFIX + "/check-runs/1"),
                                 ("PUT", "/repos/other/repo/pulls/1/merge"),
                                 ("GET", "https://evil.invalid/"),
                                 ("GET", PREFIX + "/../other"),
                                 ("POST", "/graphql")):
                with self.subTest(path=path), self.assertRaises(lab.LabError):
                    api.request(method, path, {})

    def test_http_errors_keep_status_and_permission_does_not_disappear(self):
        import urllib.error
        with patch("urllib.request.OpenerDirector.open", side_effect=urllib.error.HTTPError(
                "https://api.github.com", 403, "Forbidden", {}, io.BytesIO(b'{"message":"Forbidden"}'))):
            api = lab.GitHubAPI({"GITHUB_REPOSITORY": REPO, "GH_TOKEN": "dummy"})
            with self.assertRaises(lab.PermissionError) as cm:
                api.request("GET", PREFIX + "/pulls/1")
            self.assertEqual(cm.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
