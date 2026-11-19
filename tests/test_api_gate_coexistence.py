import copy
import importlib
import unittest
from pathlib import Path

import lab


REPO = "Zi-Yi-Ming/sr-pr-gate-lab"
PREFIX = "/repos/" + REPO
HEAD = "1" * 40
BASE = "2" * 40
MERGE = "3" * 40
CASE = {"case": "success", "pr": 31, "head": HEAD, "base": BASE, "merge": MERGE,
        "seed_run": 70, "prior_run": 71, "prior_attempt": 1}
HEAD_REF = "lab/pr-event-api-success"
BASE_REF = "lab-target/pr-event-api-success"
SOURCE_ENV = {
    "GITHUB_REPOSITORY": REPO, "GITHUB_EVENT_NAME": "workflow_dispatch",
    "GITHUB_REF": "refs/heads/lab/pr-event-api-publisher", "GITHUB_SHA": BASE,
    "PRIOR_RUN_ID": "71", "GH_TOKEN": "synthetic-token",
}


class FakeAPI:
    def __init__(self):
        def pull_ref(ref, sha):
            return {"ref": ref, "sha": sha, "repo": {"full_name": REPO}}

        def run(id_, path, suite, conclusion):
            return {"id": id_, "path": ".github/workflows/" + path, "check_suite_id": suite,
                    "event": "pull_request", "status": "completed", "conclusion": conclusion,
                    "run_attempt": 1, "head_sha": HEAD, "head_branch": HEAD_REF,
                    "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO},
                    "pull_requests": [{"number": 31}]}

        self.received = []
        self.reads = {
            "/pulls/31": {"number": 31, "state": "open", "draft": True, "merged": False,
                          "base": pull_ref(BASE_REF, BASE), "head": pull_ref(HEAD_REF, HEAD), "merge_commit_sha": MERGE},
            "/actions/runs/70": run(70, "pr-event-api-seed.yml", 80, "cancelled"),
            "/actions/runs/71": run(71, "pr-event-api-prior.yml", 81, "success"),
            "/check-suites/80": {"id": 80, "head_sha": HEAD, "status": "completed", "conclusion": "cancelled", "app": {"id": 15368}},
            "/check-suites/81": {"id": 81, "head_sha": HEAD, "status": "completed", "conclusion": "success", "app": {"id": 15368}},
            **{f"/commits/{HEAD}/check-runs?filter={f}&per_page=100": {"total_count": 0, "check_runs": []}
               for f in ("all", "latest")},
        }
        self.assigned_suite = 80

    def request(self, method, path, data=None):
        assert path.startswith(PREFIX + "/")
        route = path[len(PREFIX):]
        if method == "GET":
            return 200, copy.deepcopy(self.reads[route])
        assert (method, route) == ("POST", "/check-runs")
        self.received.append(data)
        check = {**data, "id": 90, "node_id": "synthetic", "app": {"id": 15368}, "check_suite": {"id": self.assigned_suite}}
        for filter_name in ("all", "latest"):
            self.reads[f"/commits/{HEAD}/check-runs?filter={filter_name}&per_page=100"] = {
                "total_count": 1, "check_runs": [check],
            }
        return 201, check


class CoexistenceTests(unittest.TestCase):
    def setUp(self):
        module = Path(__file__).resolve().parents[1] / "controls" / "api_gate_coexistence.py"
        self.assertTrue(module.is_file(), "API/native coexistence control is missing")
        self.coexist = importlib.import_module("controls.api_gate_coexistence")

    def receipt(self):
        return {"pr": 31, "head": HEAD, "base": BASE, "merge": MERGE, "run": 71, "attempt": 1,
                "contracts": [{"id": key, "status": "passed", "returncode": 0} for key in lab.CONTRACTS]}

    def test_four_current_qualification_fixtures(self):
        cases = [
            ("waiting", [], "ready", "awaiting-approval", [None, None]),
            ("partial", ["expensive-a"], "ready", "awaiting-approval", [0, None]),
            ("failure", ["expensive-a", "expensive-b"], "broken", "failure", [0, 0]),
            ("success", ["expensive-a", "expensive-b"], "ready", "success", [0, 0]),
        ]
        for case, grants, fixture, expected, expensive in cases:
            with self.subTest(case=case):
                result = self.coexist.evaluate(case, {"approved": grants, "fixture": fixture})
                self.assertEqual(result["verdict"], expected)
                self.assertEqual([c["returncode"] for c in result["contracts"][1:]], expensive)
                self.assertEqual(result["contracts"][0]["returncode"], 1 if case == "failure" else 0)

    def test_stale_api_gate_matches_production_identity_and_leaves_suite_selection_to_github(self):
        api = FakeAPI()
        evidence = self.coexist.publish_prior(CASE, self.receipt(), api)
        self.assertEqual(len(api.received), 1)
        check, = api.received
        self.assertEqual(check["external_id"], "semantic-router-ci:31:71:1")
        self.assertEqual((check["head_sha"], check["name"], check["conclusion"]), (HEAD, "PR Gate", "success"))
        self.assertNotIn("check_suite", check)
        self.assertTrue(evidence["matched_cancelled_suite"])

    def test_already_present_check_is_never_published_again(self):
        api = FakeAPI()
        self.coexist.publish_prior(CASE, self.receipt(), api)
        with self.assertRaisesRegex(lab.LabError, "already exists"):
            self.coexist.publish_prior(CASE, self.receipt(), api)
        self.assertEqual(len(api.received), 1)

    def test_successful_api_write_to_a_different_suite_is_an_inconclusive_observation(self):
        api = FakeAPI()
        api.assigned_suite = 81
        evidence = self.coexist.publish_prior(CASE, self.receipt(), api)
        self.assertFalse(evidence["matched_cancelled_suite"])
        self.assertEqual(len(api.received), 1)

    def test_frozen_pr_and_prior_proof_are_required_before_publishing(self):
        for source, key, value in (
            ("/pulls/31", "draft", False),
            ("/pulls/31", "merge_commit_sha", "5" * 40),
            ("/actions/runs/71", "run_attempt", 2),
            ("/actions/runs/71", "conclusion", "failure"),
            ("/check-suites/80", "conclusion", "success"),
        ):
            with self.subTest(source=source, key=key):
                api = FakeAPI()
                api.reads[source][key] = value
                with self.assertRaises(lab.LabError):
                    self.coexist.publish_prior(CASE, self.receipt(), api)
                self.assertEqual(api.received, [])
        api = FakeAPI()
        forged = self.receipt()
        forged["contracts"][0]["returncode"] = False
        with self.assertRaises(lab.LabError):
            self.coexist.publish_prior(CASE, forged, api)
        self.assertEqual(api.received, [])

    def test_trusted_publisher_has_exact_scope_and_cannot_merge_or_replace_a_check(self):
        publisher = self.coexist.PublisherAPI(CASE, SOURCE_ENV)
        payload = self.coexist.prior_payload(CASE)
        self.assertTrue(publisher.allows("POST", PREFIX + "/check-runs", payload))
        self.assertFalse(publisher.allows("POST", PREFIX + "/check-runs", {**payload, "head_sha": BASE}))
        self.assertFalse(publisher.allows("PATCH", PREFIX + "/check-runs/90", payload))
        self.assertFalse(publisher.allows("PUT", PREFIX + "/pulls/31/merge", None))
        for key, value in (("GITHUB_SHA", HEAD), ("GITHUB_EVENT_NAME", "pull_request"), ("PRIOR_RUN_ID", "999")):
            with self.subTest(key=key), self.assertRaises(lab.LabError):
                self.coexist.PublisherAPI(CASE, {**SOURCE_ENV, key: value})
