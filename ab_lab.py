"""Bounded, synthetic A/B PR Gate experiment for one explicitly named lab."""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request

import lab

BASE = "525bc8117c9176b158d206b324c904e18aab8534"
CONTROLLER_REF = "lab/ab-controller"
B2_CONTROLLER_REF = "lab/ab-b2-controller"
WORKFLOW = ".github/workflows/calibration.yml"
DISPATCH = lab.PREFIX + "/actions/workflows/calibration.yml/dispatches"
FIXTURES = ("waiting", "partial", "failure", "success")
COMMANDS = ("seed", "qualify", "relay", "publish", "baseline", "recover", "probe", "advance")
COMMON = {"schema_version", "kind", "repository", "envelope", "producer", "outcome", "all_passed", "eligible", "advance", "evidence"}
EXTRAS = {
    "qualification": {"qualification_run_id", "source_sha", "seed_run_id", "required_contracts", "contracts", "deferred", "gate_status"},
    "publication": {"qualification", "native", "initial_check", "stale_check", "before_checks", "after_checks"},
    "baseline": {"publication"},
    "recovery": {"baseline", "authoritative_check", "before_checks", "after_checks"},
    "probe": {"recovery"},
}
require = lab.require


def validate_envelope(value):
    keys = {"schema_version", "controller_ref", "controller_sha", "cases", "index", "stage"}
    require(type(value) is dict and keys <= set(value) <= keys | {"seed_run_id", "qualification_run_id"}, "invalid A/B envelope keys")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1, "invalid envelope schema")
    require(value["controller_ref"] in (CONTROLLER_REF, B2_CONTROLLER_REF) and type(value["controller_sha"]) is str
            and lab.SHA_RE.fullmatch(value["controller_sha"]), "invalid controller identity")
    require(type(value["index"]) is int and 0 <= value["index"] < 8, "index must be within the finite eight-case batch")
    if value["controller_ref"] == B2_CONTROLLER_REF:
        require(value["index"] >= 4, "B2 continuation only executes its four B cases")
    require(value["stage"] in ("seed", "qualification", "driver"), "invalid envelope stage")
    require(type(value["cases"]) is list and len(value["cases"]) == 8, "exactly eight ordered cases required")
    prs, heads = set(), set()
    for index, case in enumerate(value["cases"]):
        route, fixture = ("A" if index < 4 else "B"), FIXTURES[index % 4]
        require(type(case) is dict and set(case) == {"case_id", "route", "fixture", "pr", "head", "base", "base_ref", "head_ref"}, "invalid case keys")
        require(case["case_id"] == route + "_" + fixture and case["route"] == route and case["fixture"] == fixture, "case order/route/fixture mismatch")
        require(lab.positive(case["pr"]) and type(case["head"]) is str and lab.SHA_RE.fullmatch(case["head"]), "invalid case PR/head")
        suffix = ("b2" if value["controller_ref"] == B2_CONTROLLER_REF and route == "B" else route.lower()) + "-" + fixture
        require(case["base"] == BASE and case["base_ref"] == "lab-target/" + suffix
                and case["head_ref"] == "lab/ab-" + suffix, "case target/head refs or base changed")
        prs.add(case["pr"]); heads.add(case["head"].lower())
    require(len(prs) == 8 and len(heads) == 8 and value["controller_sha"].lower() not in heads,
            "case PRs/heads must be unique and separate from controller")
    ids = {"seed": set(), "qualification": {"seed_run_id"}, "driver": {"seed_run_id", "qualification_run_id"}}[value["stage"]]
    require(set(value) == keys | ids, "run IDs do not match stage")
    for key in ids:
        require(type(value[key]) is str and lab.POSITIVE_RE.fullmatch(value[key]), "invalid bound run ID")
    if value["stage"] == "driver":
        require(value["seed_run_id"] != value["qualification_run_id"], "seed and qualification must be separate runs")
    return value


def selected(envelope):
    return envelope["cases"][envelope["index"]]


def producer(env):
    return {"run_id": env.get("GITHUB_RUN_ID"), "run_attempt": env.get("GITHUB_RUN_ATTEMPT"),
            "sha": env.get("GITHUB_SHA"), "ref": env.get("GITHUB_REF")}


def validate_context(envelope, env, command):
    validate_envelope(envelope)
    stage = "seed" if command == "seed" else "qualification" if command in ("qualify", "relay") else "driver"
    require(envelope["stage"] == stage, "command does not match envelope stage")
    require(env.get("GITHUB_REPOSITORY") == lab.REPOSITORY and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "only lab workflow_dispatch is authorized")
    require(type(env.get("GITHUB_RUN_ID")) is str and lab.POSITIVE_RE.fullmatch(env["GITHUB_RUN_ID"])
            and env.get("GITHUB_RUN_ATTEMPT") == "1", "only original positive run IDs/attempt one are authorized")
    case = selected(envelope)
    ref = envelope["controller_ref"] if stage == "driver" else case["head_ref"]
    sha = envelope["controller_sha"] if stage == "driver" else case["head"]
    require(env.get("GITHUB_REF") == "refs/heads/" + ref and env.get("GITHUB_SHA") == sha, "source ref/SHA context mismatch")
    if env.get("GITHUB_WORKFLOW_REF"):
        require(env["GITHUB_WORKFLOW_REF"] == lab.REPOSITORY + "/" + WORKFLOW + "@refs/heads/" + ref, "workflow path context mismatch")
    if stage == "driver":
        require(env["GITHUB_RUN_ID"] not in (envelope["seed_run_id"], envelope["qualification_run_id"]), "driver must be a separate controller run")
    elif stage == "qualification":
        require(env["GITHUB_RUN_ID"] != envelope["seed_run_id"], "qualification cannot run in cancelled seed")


def next_envelope(envelope, command, run_id):
    value = copy.deepcopy(envelope)
    if command == "seed":
        value.update(stage="qualification", seed_run_id=run_id)
    elif command == "relay":
        value.update(stage="driver", qualification_run_id=run_id)
    else:
        require(value["index"] < 7, "final case cannot dispatch another case")
        value.update(stage="seed", index=value["index"] + 1)
        value.pop("seed_run_id", None); value.pop("qualification_run_id", None)
    return validate_envelope(value)


def dispatch_body(envelope):
    ref = envelope["controller_ref"] if envelope["stage"] == "driver" else selected(envelope)["head_ref"]
    return {"ref": ref, "inputs": {"phase": "publish-and-probe", "manifest": json.dumps(envelope, separators=(",", ":")), "source_run_id": ""}}


def current_external(envelope):
    return "lab-ab:current:" + selected(envelope)["case_id"] + ":" + envelope["qualification_run_id"] + ":1"


def stale_external(envelope):
    return "lab-ab:stale:" + selected(envelope)["case_id"] + ":" + envelope["seed_run_id"]


def fixture_state(fixture):
    return ("in_progress", None) if fixture in ("waiting", "partial") else ("completed", "failure" if fixture == "failure" else "success")


class API(lab.GitHubAPI):
    """Extra endpoints are constrained by the current operation, case, and run."""
    def __init__(self, command, envelope, env=None):
        env = os.environ if env is None else env
        validate_context(envelope, env, command)
        super().__init__(env)
        self.command, self.envelope, self.run_id = command, copy.deepcopy(envelope), env["GITHUB_RUN_ID"]
        self.patch_id = None
        self.dispatched = False
        self.writes = set()

    def authorize_patch(self, check):
        require(self.command == "recover" and selected(self.envelope)["route"] == "A", "PATCH only belongs to route A recovery")
        validate_check(check, self.envelope)
        self.patch_id = check["id"]

    def request(self, method, path, data=None):
        require(self._repository == lab.REPOSITORY, "lab repository binding changed")
        case, env = selected(self.envelope), self.envelope
        allowed = False
        if method == "GET":
            suffixes = (r"/check-runs/[1-9][0-9]*", r"/check-suites/[1-9][0-9]*")
            allowed = any(re.fullmatch(re.escape(lab.PREFIX) + s, path) for s in suffixes)
            allowed |= bool(re.fullmatch(re.escape(lab.PREFIX) + r"/git/commits/[0-9a-fA-F]{40}", path))
            allowed |= path == lab.PREFIX + "/git/ref/heads/" + case["base_ref"]
            allowed |= path == lab.PREFIX + "/pulls/" + str(case["pr"])
            allowed |= path in [lab.PREFIX + "/commits/" + case["head"] + "/check-runs?filter=" + f + "&per_page=100" for f in ("all", "latest")]
            allowed |= path == workflow_runs_path(env)
            for run_id in (env.get("seed_run_id"), env.get("qualification_run_id"), self.run_id):
                if run_id:
                    allowed |= path == lab.PREFIX + "/actions/runs/" + run_id
            if env.get("qualification_run_id"):
                allowed |= path == lab.PREFIX + "/actions/runs/" + env["qualification_run_id"] + "/jobs?per_page=100"
        elif method == "POST" and path == "/graphql":
            allowed = (type(data) is dict and set(data) == {"query", "variables"} and data["query"] == lab.GRAPHQL_QUERY
                       and type(data["variables"]) is dict and set(data["variables"]) == {"node", "pr", "head"}
                       and type(data["variables"]["node"]) is str and bool(data["variables"]["node"])
                       and data["variables"]["pr"] == case["pr"] and data["variables"]["head"] == case["head"])
        elif method == "POST" and path == DISPATCH and self.command in ("seed", "relay", "advance"):
            expected = dispatch_body(next_envelope(env, self.command, self.run_id))
            allowed = type(data) is dict and set(data) == {"ref", "inputs"} and data == expected and not self.dispatched
        elif method == "POST" and path == lab.PREFIX + "/actions/runs/" + self.run_id + "/cancel":
            allowed = self.command == "seed" and self.dispatched and data is None
        elif method == "POST" and path == lab.PREFIX + "/check-runs":
            expected_ids = {current_external(env)} if env["stage"] == "driver" else set()
            if self.command == "publish" and case["fixture"] != "success": expected_ids.add(stale_external(env))
            allowed = self.command == "publish" or (self.command == "recover" and case["route"] == "B")
            allowed = allowed and type(data) is dict and data.get("external_id") in expected_ids
            if allowed:
                stale = data["external_id"] == stale_external(env)
                status, conclusion = ("completed", "success") if stale else fixture_state(case["fixture"])
                keys = {"name", "head_sha", "external_id", "status", "output"} | ({"conclusion"} if conclusion else set())
                allowed = (set(data) == keys and data["name"] == "PR Gate" and data["head_sha"] == case["head"]
                           and data["status"] == status and data.get("conclusion") == conclusion
                           and type(data["output"]) is dict and set(data["output"]) == {"title", "summary"})
        elif method == "PATCH" and self.command == "recover" and case["route"] == "A" and self.patch_id is not None:
            status, conclusion = fixture_state(case["fixture"])
            allowed = (path == lab.PREFIX + "/check-runs/" + str(self.patch_id) and type(data) is dict
                       and set(data) == {"status", "output"} | ({"conclusion"} if conclusion else set())
                       and data["status"] == status and data.get("conclusion") == conclusion)
        elif method == "PUT" and self.command in ("baseline", "probe"):
            allowed = path == lab.PREFIX + "/pulls/" + str(case["pr"]) + "/merge" and data == {"sha": case["head"], "merge_method": "merge"}
        require(allowed, "A/B API operation/path/payload is outside the operation allowlist")
        if method != "GET" and path != "/graphql":
            key = (method, path, data.get("external_id") if type(data) is dict else None)
            require(key not in self.writes, "duplicate mutation within one operation is forbidden")
            self.writes.add(key)
        result = self._send(method, path, data)
        if method == "POST" and path == DISPATCH and result[0] == 204: self.dispatched = True
        return result

    def _send(self, method, path, data):
        body = None if data is None else json.dumps(data).encode("utf-8")
        req = urllib.request.Request("https://api.github.com" + path, data=body, method=method, headers={
            "Authorization": "Bearer " + self._token, "Accept": "application/vnd.github+json", "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "sr-pr-gate-lab-ab"})
        try:
            with self._opener.open(req, timeout=30) as response: status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc: status, raw = exc.code, exc.read()
        except (urllib.error.URLError, OSError) as exc: raise lab.LabError("API transport failed without retry") from exc
        try: parsed = lab.parse_json(raw)
        except (ValueError, UnicodeDecodeError): parsed = {"message": "API returned a non-JSON body"}
        if status in (401, 403): raise lab.PermissionError(status, parsed)
        return status, parsed


def workflow_runs_path(envelope):
    return lab.PREFIX + "/actions/workflows/calibration.yml/runs?head_sha=" + selected(envelope)["head"] + "&event=workflow_dispatch&per_page=100"


def request(client, method, path, data=None, evidence=None):
    try:
        status, body = lab.call(client, method, path, data)
    except lab.PermissionError as exc:
        if evidence is not None: evidence.update(status=exc.status, body=exc.body)
        raise
    if evidence is not None: evidence.update(status=status, body=body)
    return status, body


def read(client, path):
    status, body = request(client, "GET", path)
    require(status == 200 and type(body) is dict, "read requires an HTTP 200 JSON object")
    return body


def bounded(body, key):
    count, values = body.get("total_count"), body.get(key)
    require(type(count) is int and 0 <= count <= 100 and type(values) is list and len(values) == count
            and all(type(item) is dict for item in values), "bounded API list is malformed or truncated")
    return values


def checks(client, envelope, filter_name="all"):
    return bounded(read(client, lab.PREFIX + "/commits/" + selected(envelope)["head"] + "/check-runs?filter=" + filter_name + "&per_page=100"), "check_runs")


def validate_pr(body, case, merged=False):
    require(type(body) is dict and body.get("number") == case["pr"] and body.get("draft") is False, "PR identity/draft mismatch")
    require(body.get("head", {}).get("sha") == case["head"] and body["head"].get("ref") == case["head_ref"]
            and body["head"].get("repo", {}).get("full_name") == lab.REPOSITORY, "PR head identity mismatch")
    require(body.get("base", {}).get("ref") == case["base_ref"] and body["base"].get("repo", {}).get("full_name") == lab.REPOSITORY,
            "PR target is not the dedicated protected lab branch")
    if merged:
        require(body.get("state") == "closed" and body.get("merged") is True and bool(body.get("merged_at")), "merged PR readback is inconsistent")
    else:
        require(body["base"].get("sha") == case["base"] and body.get("state") == "open"
                and body.get("merged") is False and not body.get("merged_at"), "PR is not open on frozen base")


def validate_target_ref(body, envelope, frozen=True):
    case = selected(envelope)
    require(type(body) is dict and body.get("ref") == "refs/heads/" + case["base_ref"]
            and body.get("object", {}).get("type") == "commit", "live target ref identity mismatch")
    if frozen: require(body["object"].get("sha") == case["base"], "live target branch advanced from frozen base")


def frozen_target(client, envelope, target, key="base_ref"):
    target[key] = read(client, lab.PREFIX + "/git/ref/heads/" + selected(envelope)["base_ref"])
    validate_target_ref(target[key], envelope)


def record_merge_commit(client, envelope, merge, snapshot):
    sha = merge.get("body", {}).get("sha")
    snapshot["merge_commit"] = None
    if type(sha) is str and lab.SHA_RE.fullmatch(sha):
        snapshot["merge_commit"] = read(client, lab.PREFIX + "/git/commits/" + sha)


def validate_merge_commit(snapshot, envelope, merge):
    case = selected(envelope)
    sha, commit = merge.get("body", {}).get("sha"), snapshot.get("merge_commit")
    require(type(sha) is str and lab.SHA_RE.fullmatch(sha) and type(commit) is dict and commit.get("sha") == sha,
            "ordinary merge commit identity is not confirmed")
    require(type(commit.get("parents")) is list and [p.get("sha") for p in commit["parents"]] == [case["base"], case["head"]],
            "merge commit parents differ from frozen base/head")
    validate_target_ref(snapshot["base_ref"], envelope, frozen=False)
    require(snapshot["base_ref"]["object"].get("sha") == sha, "target branch does not point at the confirmed merge")


def validate_check(check, envelope, stale=False, receipt=None):
    case = selected(envelope)
    status, conclusion = ("completed", "success") if stale else fixture_state(case["fixture"])
    require(type(check) is dict and check.get("name") == "PR Gate" and check.get("head_sha") == case["head"]
            and check.get("external_id") == (stale_external(envelope) if stale else current_external(envelope)), "Gate canonical identity mismatch")
    require(check.get("app", {}).get("id") == lab.APP_ID and lab.positive(check.get("id"))
            and type(check.get("node_id")) is str and bool(check["node_id"]) and lab.positive(check.get("check_suite", {}).get("id")), "Gate owner/receipt identity mismatch")
    require(check.get("status") == status and check.get("conclusion") == conclusion, "Gate state contradicts immutable qualification")
    if receipt is not None:
        require(all(check.get(k) == receipt.get(k) for k in ("id", "node_id", "check_suite")), "exact Gate receipt changed")


def fingerprint(check):
    require(type(check) is dict and lab.positive(check.get("id")), "invalid check snapshot")
    return {"id": check["id"], "node_id": check.get("node_id"), "app": check.get("app", {}).get("id"),
            "head": check.get("head_sha"), "name": check.get("name"), "status": check.get("status"),
            "conclusion": check.get("conclusion"), "external": check.get("external_id"), "suite": check.get("check_suite", {}).get("id")}


def unchanged(before, after, changed=(), added=()):
    a, b = {c["id"]: fingerprint(c) for c in before}, {c["id"]: fingerprint(c) for c in after}
    require(len(a) == len(before) and len(b) == len(after), "duplicate snapshot check IDs")
    require(set(b) == set(a) | set(added), "unexpected checks added or removed")
    require(all(b[key] == value for key, value in a.items() if key not in changed), "foreign/native/stale check fields changed")


def common(document, kind, envelope=None, env=None):
    require(type(document) is dict and set(document) == COMMON | EXTRAS.get(kind, set()), "invalid " + kind + " artifact keys")
    require(type(document["schema_version"]) is int and document["schema_version"] == 1
            and document["kind"] == "ab-" + kind and document["repository"] == lab.REPOSITORY
            and document["all_passed"] is True and type(document["eligible"]) is bool and type(document["advance"]) is bool
            and type(document["evidence"]) is dict, "incomplete or untrusted " + kind + " artifact")
    e = validate_envelope(document["envelope"])
    p = document["producer"]
    require(type(p) is dict and set(p) == {"run_id", "run_attempt", "sha", "ref"} and type(p["run_id"]) is str
            and lab.POSITIVE_RE.fullmatch(p["run_id"]) and p["run_attempt"] == "1", "invalid artifact producer")
    case = selected(e)
    require(p["sha"] == (e["controller_sha"] if e["stage"] == "driver" else case["head"])
            and p["ref"] == "refs/heads/" + (e["controller_ref"] if e["stage"] == "driver" else case["head_ref"]), "artifact producer source mismatch")
    if envelope is not None: require(e == envelope, "artifact does not match current batch/case/run IDs")
    if env is not None: require(p == producer(env), "driver artifact is from a different run/attempt/source")
    return e


def validate_qualification(document, envelope):
    e = common(document, "qualification")
    expected = copy.deepcopy(envelope); expected["stage"] = "qualification"; expected.pop("qualification_run_id")
    require(e == expected and document["qualification_run_id"] == envelope["qualification_run_id"]
            and document["producer"]["run_id"] == envelope["qualification_run_id"]
            and document["seed_run_id"] == envelope["seed_run_id"] and document["source_sha"] == selected(envelope)["head"], "qualification source/run binding mismatch")
    require(document["outcome"] == "QUALIFIED" and not document["eligible"] and not document["advance"], "invalid qualification outcome")
    fixture = selected(envelope)["fixture"]
    deferred = lab.deferred_for(fixture)
    require(document["required_contracts"] == list(lab.CONTRACTS) and document["deferred"] == deferred
            and type(document["contracts"]) is list and len(document["contracts"]) == 3, "immutable contract inventory changed")
    for item, contract in zip(document["contracts"], lab.CONTRACTS):
        require(type(item) is dict and set(item) == {"id", "status", "execution"} and item["id"] == contract, "invalid contract receipt")
        if contract in deferred:
            require(item["status"] == "deferred" and item["execution"] is None, "deferred contract was upgraded")
        else:
            execution = item["execution"]
            require(type(execution) is dict and set(execution) == {"returncode", "stdout", "stderr"}
                    and type(execution["returncode"]) is int and all(type(execution[k]) is str for k in ("stdout", "stderr")), "invalid fixed subprocess receipt")
            code = 1 if fixture == "failure" and contract == "automatic" else 0
            require(execution["returncode"] == code and item["status"] == ("failed" if code else "passed"), "fixed process result was changed")
    require(document["gate_status"] == lab.derive_status(document["contracts"]), "gate status was upgraded without qualification")
    return document


def validate_suite(suite, suite_id, case, conclusion=None):
    require(type(suite) is dict and suite.get("id") == suite_id and suite.get("head_sha") == case["head"]
            and suite.get("app", {}).get("id") == lab.APP_ID, "native/Gate suite identity mismatch")
    if conclusion is not None:
        require(suite.get("status") == "completed" and suite.get("conclusion") == conclusion, "suite is not in the expected terminal state")


def validate_native(native, envelope, *, allow_seed_suite_change=False):
    case = selected(envelope)
    for key, id_key, expected in (("seed_run", "seed_run_id", "cancelled"),
            ("qualification_run", "qualification_run_id", "failure" if case["fixture"] == "failure" else "success")):
        run = native[key]
        require(run.get("id") == int(envelope[id_key]) and run.get("path") == WORKFLOW and run.get("event") == "workflow_dispatch"
                and run.get("head_branch") == case["head_ref"] and run.get("head_sha") == case["head"]
                and type(run.get("run_attempt")) is int and run["run_attempt"] == 1
                and run.get("repository", {}).get("full_name") == lab.REPOSITORY
                and run.get("head_repository", {}).get("full_name") == lab.REPOSITORY, "native workflow run provenance mismatch")
        require(run.get("status") == "completed" and run.get("conclusion") == expected, "native workflow is not completed with expected conclusion")
        require(lab.positive(run.get("check_suite_id")), "native suite ID missing")
        suite_expected = None if key == "seed_run" and allow_seed_suite_change else expected
        validate_suite(native["seed_suite" if key == "seed_run" else "qualification_suite"], run["check_suite_id"], case, suite_expected)
    require(native["seed_run"]["check_suite_id"] != native["qualification_run"]["check_suite_id"], "seed/qualification suites must differ")
    jobs = native["jobs"]
    require(type(jobs) is list and all(type(j) is dict for j in jobs), "invalid native jobs")
    aggregate = [j for j in jobs if j.get("name") == "CI Execution"]
    expected = "failure" if case["fixture"] == "failure" else "success"
    require(len(aggregate) == 1 and aggregate[0].get("status") == "completed" and aggregate[0].get("conclusion") == expected,
            "native CI Execution did not match actual fixture result")
    for job in jobs:
        require(job.get("status") == "completed", "native job is still incomplete")
        allowed = {"success", "skipped"}
        if case["fixture"] == "failure" and job.get("name") in ("CI Execution", "Native qualification"): allowed.add("failure")
        require(job.get("conclusion") in allowed, "unexpected failure/cancellation in native qualification")
    require(type(native["workflow_runs"]) is list, "invalid bounded workflow run inventory")
    head_runs = [r for r in native["workflow_runs"] if r.get("head_sha") == case["head"]]
    require(len(head_runs) == 2 and {r.get("id") for r in head_runs}
            == {int(envelope["seed_run_id"]), int(envelope["qualification_run_id"])}, "duplicate or missing head-bound experiment runs")


def fetch_native(client, envelope, native, *, allow_seed_suite_change=False):
    for key, id_key in (("seed_run", "seed_run_id"), ("qualification_run", "qualification_run_id")):
        native[key] = read(client, lab.PREFIX + "/actions/runs/" + envelope[id_key])
    for key, run_key in (("seed_suite", "seed_run"), ("qualification_suite", "qualification_run")):
        native[key] = read(client, lab.PREFIX + "/check-suites/" + str(native[run_key].get("check_suite_id")))
    native["jobs"] = bounded(read(client, lab.PREFIX + "/actions/runs/" + envelope["qualification_run_id"] + "/jobs?per_page=100"), "jobs")
    native["workflow_runs"] = bounded(read(client, workflow_runs_path(envelope)), "workflow_runs")
    validate_native(native, envelope, allow_seed_suite_change=allow_seed_suite_change)


def validate_publication(document, envelope, env=None):
    common(document, "publication", envelope, env)
    require(document["outcome"] == "PUBLISHED" and not document["eligible"] and not document["advance"], "invalid publication outcome")
    validate_qualification(document["qualification"], envelope)
    validate_native(document["native"], envelope)
    validate_check(document["initial_check"], envelope)
    stale = document["stale_check"]
    if selected(envelope)["fixture"] == "success": require(stale is None, "positive must not have a stale fixture")
    else: validate_check(stale, envelope, stale=True)
    added = [document["initial_check"]["id"]] + ([] if stale is None else [stale["id"]])
    unchanged(document["before_checks"], document["after_checks"], added=added)
    by_id = {c["id"]: c for c in document["after_checks"]}
    validate_check(by_id.get(document["initial_check"]["id"]), envelope, receipt=document["initial_check"])
    if stale is not None: validate_check(by_id.get(stale["id"]), envelope, stale=True, receipt=stale)
    return document


def graphql_data(snapshot, envelope, current, required=True):
    result = snapshot["graphql"]
    require(result["status"] == 200 and type(result["body"]) is dict and not result["body"].get("errors"), "GraphQL evidence unavailable")
    data = result["body"].get("data", {})
    require(data.get("check", {}).get("id") == current["node_id"], "GraphQL exact check node mismatch")
    if required: require(data["check"].get("isRequired") is True, "authoritative Gate is not confirmed required")
    repository = data.get("repository", {})
    require(repository.get("pullRequest", {}).get("headRefOid") == selected(envelope)["head"]
            and repository.get("object", {}).get("oid") == selected(envelope)["head"], "GraphQL head binding mismatch")
    return data


def snapshot(client, envelope, publication, current, target, *, required=True, state="open"):
    case = selected(envelope)
    target["base_ref"] = read(client, lab.PREFIX + "/git/ref/heads/" + case["base_ref"])
    validate_target_ref(target["base_ref"], envelope, frozen=state == "open")
    target["pr"] = read(client, lab.PREFIX + "/pulls/" + str(case["pr"]))
    if state != "raw": validate_pr(target["pr"], case, merged=state == "merged")
    target["exact_check"] = read(client, lab.PREFIX + "/check-runs/" + str(current["id"]))
    validate_check(target["exact_check"], envelope, receipt=current)
    for f in ("all", "latest"):
        target[f + "_checks"] = checks(client, envelope, f)
        matches = [c for c in target[f + "_checks"] if c.get("id") == current["id"]]
        require(len(matches) == 1, "authoritative Gate is absent/duplicated in " + f)
        validate_check(matches[0], envelope, receipt=current)
    target["graphql"] = {}
    request(client, "POST", "/graphql", {"query": lab.GRAPHQL_QUERY, "variables": {"node": current["node_id"], "pr": case["pr"], "head": case["head"]}}, target["graphql"])
    graphql_data(target, envelope, current, required)
    for key, suite_id in (("gate_suite", current["check_suite"]["id"]),
            ("seed_suite", publication["native"]["seed_run"]["check_suite_id"]),
            ("qualification_suite", publication["native"]["qualification_run"]["check_suite_id"])):
        target[key] = read(client, lab.PREFIX + "/check-suites/" + str(suite_id))
        validate_suite(target[key], suite_id, case)


def target_topology(snapshot, envelope, publication):
    current = publication["initial_check"]
    data = graphql_data(snapshot, envelope, current, False)
    state = rollup_state(data)
    return (data["check"].get("isRequired") is True and current["status"] == "completed" and current["conclusion"] == "success"
            and current["check_suite"]["id"] == publication["native"]["seed_run"]["check_suite_id"]
            and snapshot["gate_suite"].get("status") == "completed" and snapshot["gate_suite"].get("conclusion") == "cancelled"
            and snapshot["seed_suite"].get("status") == "completed" and snapshot["seed_suite"].get("conclusion") == "cancelled"
            and snapshot["qualification_suite"].get("status") == "completed" and snapshot["qualification_suite"].get("conclusion") == "success"
            and state == "FAILURE")


def rollup_state(data):
    rollup = data["repository"]["object"].get("statusCheckRollup")
    require(type(rollup) is dict and type(rollup.get("state")) is str and bool(rollup["state"]),
            "head statusCheckRollup is unavailable; cannot classify recovery")
    return rollup["state"]


def merge_only_positive(envelope):
    return envelope["controller_ref"] == B2_CONTROLLER_REF and envelope["index"] == 7


def validate_merge_only_structure(snapshot, envelope, publication):
    require(merge_only_positive(envelope), "merge-only positive evidence requires the fixed B2 success case")
    current = publication["initial_check"]; case = selected(envelope)
    graphql_data(snapshot, envelope, current, required=True)
    require(publication["qualification"]["gate_status"] == "success"
            and not publication["qualification"]["deferred"]
            and all(c["status"] == "passed" for c in publication["qualification"]["contracts"]),
            "merge-only positive requires complete current successful qualification")
    require(current["check_suite"]["id"] == publication["native"]["seed_run"]["check_suite_id"],
            "original Gate does not belong to cancelled seed suite")
    validate_suite(snapshot["gate_suite"], current["check_suite"]["id"], case, "cancelled")
    validate_suite(snapshot["seed_suite"], publication["native"]["seed_suite"]["id"], case, "cancelled")
    validate_suite(snapshot["qualification_suite"], publication["native"]["qualification_suite"]["id"], case, "success")


def merge_only_scope(evidence):
    require(evidence.get("evidence_scope") == "MERGE_DECISION_ONLY"
            and evidence.get("strict_recovery_verified") is False,
            "limited merge evidence cannot claim strict recovery")


def gate_refusal(evidence):
    body = evidence.get("body")
    message = body.get("message", "") if type(body) is dict else ""
    return (evidence.get("status") == 405 and type(message) is str
            and re.fullmatch(r'Required status check (?:"PR Gate"|PR Gate) is (?:expected|failing|in progress)\.?',
                             message, flags=re.IGNORECASE) is not None)


def merged_response(evidence):
    return evidence.get("status") == 200 and type(evidence.get("body")) is dict and evidence["body"].get("merged") is True


def negative_safe(snapshot, case):
    if snapshot["pr"].get("merged") is True or snapshot["pr"].get("merged_at"):
        raise lab.UnsafeError("negative fixture merged according to postread; stopped")
    validate_pr(snapshot["pr"], case)
    require(snapshot["base_ref"]["object"].get("sha") == case["base"], "negative target branch changed despite stale PR base")


def validate_baseline(document, envelope, env=None, eligible=False):
    common(document, "baseline", envelope, env)
    publication = validate_publication(document["publication"], envelope, env)
    before = document["evidence"]["before"]
    validate_pr(before["pr"], selected(envelope))
    validate_check(before["exact_check"], envelope, receipt=publication["initial_check"])
    unchanged(publication["after_checks"], before["all_checks"])
    if eligible:
        limited = merge_only_positive(envelope)
        require(document["outcome"] == ("ELIGIBLE_MERGE_PROBE_ONLY" if limited else "ELIGIBLE")
                and document["eligible"] is True and not document["advance"], "baseline does not authorize recovery")
        require(gate_refusal(document["evidence"]["merge"]), "eligible baseline lacks actual required-Gate HTTP 405")
        negative_safe(document["evidence"]["after"], selected(envelope))
        graphql_data(before, envelope, publication["initial_check"])
        if limited:
            merge_only_scope(document["evidence"])
            validate_merge_only_structure(before, envelope, publication)
        elif selected(envelope)["fixture"] == "success":
            require(target_topology(before, envelope, publication), "target cancelled-seed topology was not reproduced")
    return publication


def validate_recovery(document, envelope, env=None):
    common(document, "recovery", envelope, env)
    require(document["outcome"] == "RECOVERY_APPLIED" and not document["eligible"] and not document["advance"], "invalid recovery outcome")
    publication = validate_baseline(document["baseline"], envelope, env, eligible=True)
    current = document["authoritative_check"]
    validate_check(current, envelope)
    old = publication["initial_check"]
    if selected(envelope)["route"] == "A":
        validate_check(current, envelope, receipt=old)
        unchanged(document["before_checks"], document["after_checks"], changed=(old["id"],))
    else:
        require(current["id"] != old["id"], "route B must create a new current check")
        unchanged(document["before_checks"], document["after_checks"], added=(current["id"],))
    unchanged(publication["after_checks"], document["before_checks"])
    return publication


def qualify(report, envelope):
    case = selected(envelope); contracts = []; deferred = lab.deferred_for(case["fixture"])
    report.update(qualification_run_id=report["producer"]["run_id"], source_sha=case["head"], seed_run_id=envelope["seed_run_id"],
                  required_contracts=list(lab.CONTRACTS), contracts=contracts, deferred=deferred)
    for contract in lab.CONTRACTS:
        if contract in deferred: item = {"id": contract, "status": "deferred", "execution": None}
        else:
            code = lab.FAIL_CODE if case["fixture"] == "failure" and contract == "automatic" else lab.PASS_CODE
            result = subprocess.run([sys.executable, "-I", "-c", code], shell=False, capture_output=True, text=True, timeout=30, check=False)
            item = {"id": contract, "status": "passed" if result.returncode == 0 else "failed", "execution": {
                    "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}}
        contracts.append(item)
    report.update(gate_status=lab.derive_status(contracts), outcome="QUALIFIED", all_passed=True)
    driver = copy.deepcopy(envelope); driver.update(stage="driver", qualification_run_id=report["qualification_run_id"])
    validate_qualification(report, driver)


def check_payload(envelope, qualification, stale=False, patch=False):
    # Revalidate the subprocess evidence before every mutation that can affect Gate.
    validate_qualification(qualification, envelope)
    case = selected(envelope)
    status, conclusion = ("completed", "success") if stale else lab.expected_state(qualification)
    payload = {"status": status, "output": {"title": "LAB A/B " + ("stale fixture" if stale else "current qualification"),
        "summary": "Explicit synthetic stale success fixture; NOT qualification evidence." if stale else
        "LAB synthetic " + case["case_id"] + ": " + qualification["gate_status"] + "; deferred=" + ",".join(qualification["deferred"])}}
    if not patch: payload.update(name="PR Gate", head_sha=case["head"], external_id=stale_external(envelope) if stale else current_external(envelope))
    if conclusion is not None: payload["conclusion"] = conclusion
    return payload


def publish(report, envelope, client, document):
    validate_qualification(document, envelope)
    report.update(qualification=copy.deepcopy(document), native={}, initial_check=None, stale_check=None, before_checks=[], after_checks=[])
    fetch_native(client, envelope, report["native"])
    case = selected(envelope)
    report["evidence"]["pr"] = read(client, lab.PREFIX + "/pulls/" + str(case["pr"]))
    validate_pr(report["evidence"]["pr"], case)
    report["before_checks"] = checks(client, envelope)
    require(not any(c.get("name") == "PR Gate" and str(c.get("external_id", "")).startswith(("lab-ab:", "sr-pr-gate-lab:")) for c in report["before_checks"]), "head already has lab Gate artifacts; refusing duplicate publication")
    for stale in ([True, False] if case["fixture"] != "success" else [False]):
        key = "stale_check" if stale else "initial_check"
        report["evidence"][key + "_post"] = {}
        frozen_target(client, envelope, report["evidence"], "base_ref_before_" + key)
        status, check = request(client, "POST", lab.PREFIX + "/check-runs", check_payload(envelope, document, stale), report["evidence"][key + "_post"])
        report[key] = check
        require(status == 201, "Gate POST did not return HTTP 201")
        validate_check(check, envelope, stale)
    report["after_checks"] = checks(client, envelope)
    report.update(outcome="PUBLISHED", all_passed=True)
    validate_publication(report, envelope)


def baseline(report, envelope, client, document, env):
    publication = validate_publication(document, envelope, env)
    report["publication"] = copy.deepcopy(publication)
    case = selected(envelope); before = report["evidence"]["before"] = {}
    snapshot(client, envelope, publication, publication["initial_check"], before, required=case["fixture"] != "success")
    unchanged(publication["after_checks"], before["all_checks"])
    if case["fixture"] != "success":
        validate_check(next((c for c in before["all_checks"] if c.get("id") == publication["stale_check"]["id"]), None), envelope, True, publication["stale_check"])
    elif merge_only_positive(envelope):
        report["evidence"].update(evidence_scope="MERGE_DECISION_ONLY", strict_recovery_verified=False)
        validate_merge_only_structure(before, envelope, publication)
    else:
        reproduced = target_topology(before, envelope, publication)
        report["evidence"]["topology_reproduced"] = reproduced
        if not reproduced:
            report.update(outcome="NOT_REPRODUCED", all_passed=True, advance=True)
            return
    evidence = report["evidence"]["merge"] = {}
    frozen_target(client, envelope, report["evidence"], "base_ref_before_merge")
    _, body = request(client, "PUT", lab.PREFIX + "/pulls/" + str(case["pr"]) + "/merge", {"sha": case["head"], "merge_method": "merge"}, evidence)
    if case["fixture"] != "success" and type(body) is dict and body.get("merged") is True: raise lab.UnsafeError("negative baseline unexpectedly merged; stopped")
    after = report["evidence"]["after"] = {}
    snapshot(client, envelope, publication, publication["initial_check"], after, required=False, state="raw")
    unchanged(before["all_checks"], after["all_checks"])
    if case["fixture"] == "success" and merged_response(evidence):
        record_merge_commit(client, envelope, evidence, after)
        validate_merge_commit(after, envelope, evidence)
        validate_pr(after["pr"], case, merged=True)
        report.update(outcome="BASELINE_MERGED_NO_RECOVERY_NEEDED" if merge_only_positive(envelope) else "NOT_REPRODUCED", all_passed=True, advance=True)
        return
    negative_safe(after, case)
    require(gate_refusal(evidence), "ordinary merge refusal is not attributable to required PR Gate (HTTP 405)")
    report.update(outcome="ELIGIBLE_MERGE_PROBE_ONLY" if merge_only_positive(envelope) else "ELIGIBLE", all_passed=True, eligible=True)


def recover(report, envelope, client, document, env):
    publication = validate_baseline(document, envelope, env, eligible=True)
    report.update(baseline=copy.deepcopy(document), authoritative_check=None, before_checks=[], after_checks=[])
    if envelope["controller_ref"] == B2_CONTROLLER_REF:
        report["evidence"]["native_before_mutation"] = {}
        fetch_native(client, envelope, report["evidence"]["native_before_mutation"])
    before = report["evidence"]["before"] = {}
    old = publication["initial_check"]
    snapshot(client, envelope, publication, old, before)
    unchanged(publication["after_checks"], before["all_checks"])
    report["before_checks"] = copy.deepcopy(before["all_checks"])
    evidence = report["evidence"]["mutation"] = {}
    frozen_target(client, envelope, report["evidence"], "base_ref_before_mutation")
    if selected(envelope)["route"] == "A":
        if isinstance(client, API): client.authorize_patch(before["exact_check"])
        status, current = request(client, "PATCH", lab.PREFIX + "/check-runs/" + str(old["id"]), check_payload(envelope, publication["qualification"], patch=True), evidence)
        report["authoritative_check"] = current
        require(status == 200, "route A PATCH did not return HTTP 200")
        validate_check(current, envelope, receipt=old)
    else:
        status, current = request(client, "POST", lab.PREFIX + "/check-runs", check_payload(envelope, publication["qualification"]), evidence)
        report["authoritative_check"] = current
        require(status == 201, "route B POST did not return HTTP 201")
        validate_check(current, envelope)
        require(current["id"] != old["id"], "route B reused old check ID")
    after = report["evidence"]["after"] = {}
    snapshot(client, envelope, publication, current, after)
    report["after_checks"] = copy.deepcopy(after["all_checks"])
    report.update(outcome="RECOVERY_APPLIED", all_passed=True)
    validate_recovery(report, envelope, env)


def positive_readback(snapshot, envelope, current, merge):
    validate_pr(snapshot["pr"], selected(envelope), merged=True)
    validate_merge_commit(snapshot, envelope, merge)
    data = graphql_data(snapshot, envelope, current, required=False)
    validate_suite(snapshot["gate_suite"], current["check_suite"]["id"], selected(envelope), "success")
    require(rollup_state(data) == "SUCCESS", "postmerge head rollup is not SUCCESS")


def probe(report, envelope, client, document, env):
    publication = validate_recovery(document, envelope, env)
    report["recovery"] = copy.deepcopy(document)
    case, current = selected(envelope), document["authoritative_check"]
    if envelope["controller_ref"] == B2_CONTROLLER_REF:
        report["evidence"]["native_before_merge"] = {}
        fetch_native(client, envelope, report["evidence"]["native_before_merge"], allow_seed_suite_change=True)
    if merge_only_positive(envelope):
        report["evidence"].update(evidence_scope="MERGE_DECISION_ONLY", strict_recovery_verified=False)
    before = report["evidence"]["before"] = {}
    snapshot(client, envelope, publication, current, before)
    unchanged(document["after_checks"], before["all_checks"])
    evidence = report["evidence"]["merge"] = {}
    frozen_target(client, envelope, report["evidence"], "base_ref_before_merge")
    _, body = request(client, "PUT", lab.PREFIX + "/pulls/" + str(case["pr"]) + "/merge", {"sha": case["head"], "merge_method": "merge"}, evidence)
    if case["fixture"] != "success" and type(body) is dict and body.get("merged") is True: raise lab.UnsafeError("negative recovery unexpectedly merged; stopped")
    after = report["evidence"]["after"] = {}
    snapshot(client, envelope, publication, current, after, required=False, state="raw")
    unchanged(before["all_checks"], after["all_checks"])
    if case["fixture"] == "success" and merged_response(evidence):
        record_merge_commit(client, envelope, evidence, after)
        if merge_only_positive(envelope):
            validate_pr(after["pr"], case, merged=True)
            validate_merge_commit(after, envelope, evidence)
            report.update(outcome="MERGED_PROBE_ONLY", all_passed=True, advance=True)
            return
        try:
            positive_readback(after, envelope, current, evidence)
            outcome = "RECOVERED"
        except lab.PermissionError: raise
        except lab.LabError as exc:
            report["evidence"]["readback_mismatch"] = str(exc)
            outcome = "MERGED_INCONSISTENT_READBACK"
        report.update(outcome=outcome, all_passed=True, advance=True)
        return
    negative_safe(after, case)
    require(gate_refusal(evidence), "ordinary merge refusal is not attributable to required PR Gate (HTTP 405)")
    outcome = "STILL_BLOCKED_PROBE_ONLY" if merge_only_positive(envelope) else "NOT_RECOVERED" if case["fixture"] == "success" else "PASS"
    report.update(outcome=outcome, all_passed=True, advance=True)


def validate_advance(document, envelope, env):
    case = selected(envelope)
    if document.get("kind") == "ab-baseline":
        publication = validate_baseline(document, envelope, env)
        if merge_only_positive(envelope):
            require(document["outcome"] == "BASELINE_MERGED_NO_RECOVERY_NEEDED"
                    and document["advance"] is True and not document["eligible"], "B2 baseline cannot advance")
            merge_only_scope(document["evidence"])
            validate_merge_only_structure(document["evidence"]["before"], envelope, publication)
            require(merged_response(document["evidence"]["merge"]), "limited baseline lacks actual merge")
            validate_pr(document["evidence"]["after"]["pr"], case, merged=True)
            validate_merge_commit(document["evidence"]["after"], envelope, document["evidence"]["merge"])
            return
        require(case["fixture"] == "success" and document["outcome"] == "NOT_REPRODUCED"
                and document["advance"] is True and not document["eligible"], "baseline cannot advance")
        before = document["evidence"]["before"]
        if target_topology(before, envelope, publication):
            require(merged_response(document["evidence"].get("merge", {})), "reproduced topology requires a verified ordinary merge to advance")
            validate_pr(document["evidence"]["after"]["pr"], case, merged=True)
            validate_merge_commit(document["evidence"]["after"], envelope, document["evidence"]["merge"])
        else:
            require(document["evidence"].get("topology_reproduced") is False and "merge" not in document["evidence"], "NOT_REPRODUCED proof is missing")
    else:
        common(document, "probe", envelope, env)
        publication = validate_recovery(document["recovery"], envelope, env)
        current = document["recovery"]["authoritative_check"]
        require(document["advance"] is True and not document["eligible"], "probe does not authorize advance")
        evidence = document["evidence"]
        validate_pr(evidence["before"]["pr"], case)
        validate_check(evidence["before"]["exact_check"], envelope, receipt=current)
        graphql_data(evidence["before"], envelope, current, required=True)
        unchanged(document["recovery"]["after_checks"], evidence["before"]["all_checks"])
        unchanged(evidence["before"]["all_checks"], evidence["after"]["all_checks"])
        if case["fixture"] != "success":
            require(document["outcome"] == "PASS" and gate_refusal(evidence["merge"]), "negative PASS lacks required-Gate refusal")
            negative_safe(evidence["after"], case)
        elif merge_only_positive(envelope):
            merge_only_scope(evidence)
            if document["outcome"] == "STILL_BLOCKED_PROBE_ONLY":
                require(gate_refusal(evidence["merge"]), "limited blocked result lacks exact Gate405")
                negative_safe(evidence["after"], case)
            else:
                require(document["outcome"] == "MERGED_PROBE_ONLY" and merged_response(evidence["merge"]),
                        "limited result cannot be upgraded to strict recovery")
                validate_pr(evidence["after"]["pr"], case, merged=True)
                validate_merge_commit(evidence["after"], envelope, evidence["merge"])
        elif document["outcome"] == "NOT_RECOVERED":
            require(gate_refusal(evidence["merge"]), "NOT_RECOVERED lacks required-Gate refusal")
            validate_pr(evidence["after"]["pr"], case)
        else:
            require(merged_response(evidence["merge"]), "positive terminal outcome lacks HTTP 200 merged:true")
            if document["outcome"] == "RECOVERED": positive_readback(evidence["after"], envelope, current, evidence["merge"])
            else:
                require(document["outcome"] == "MERGED_INCONSISTENT_READBACK", "unknown positive probe outcome")
                try: positive_readback(evidence["after"], envelope, current, evidence["merge"])
                except lab.LabError: pass
                else: raise lab.LabError("inconsistent-readback outcome contradicts evidence")


def execute(command, envelope, client=None, document=None, env=None):
    env = os.environ if env is None else env
    kind = "qualification" if command == "qualify" else "publication" if command == "publish" else "recovery" if command == "recover" else command
    report = {"schema_version": 1, "kind": "ab-" + kind, "repository": lab.REPOSITORY,
              "envelope": copy.deepcopy(envelope), "producer": producer(env), "outcome": "INCONCLUSIVE",
              "all_passed": False, "eligible": False, "advance": False, "evidence": {}}
    try:
        require(command in COMMANDS, "unknown command")
        validate_context(envelope, env, command)
        if command == "qualify": qualify(report, envelope)
        elif command in ("seed", "relay"):
            if command == "seed":
                report["evidence"]["before_checks"] = checks(client, envelope)
                require(not any(c.get("name") == "PR Gate" and str(c.get("external_id", "")).startswith(("lab-ab:", "sr-pr-gate-lab:")) for c in report["evidence"]["before_checks"]), "head already contains lab Gate artifacts")
            nxt = next_envelope(envelope, command, env["GITHUB_RUN_ID"])
            evidence = report["evidence"]["dispatch"] = {}
            status, _ = request(client, "POST", DISPATCH, dispatch_body(nxt), evidence)
            require(status == 204, "workflow dispatch was not HTTP 204")
            report["evidence"]["dispatched_envelope"] = nxt
            if command == "seed":
                evidence = report["evidence"]["self_cancel"] = {}
                status, _ = request(client, "POST", lab.PREFIX + "/actions/runs/" + env["GITHUB_RUN_ID"] + "/cancel", evidence=evidence)
                require(status == 202, "self-cancel request was not HTTP 202; cancellation is not established")
            report.update(outcome="SEED_CANCEL_REQUESTED" if command == "seed" else "RELAYED", all_passed=True)
        elif command == "publish": publish(report, envelope, client, document)
        elif command == "baseline": baseline(report, envelope, client, document, env)
        elif command == "recover": recover(report, envelope, client, document, env)
        elif command == "probe": probe(report, envelope, client, document, env)
        else:
            validate_advance(document, envelope, env)
            if envelope["index"] == 7: report.update(outcome="BATCH_COMPLETE", all_passed=True, batch_complete=True)
            else:
                nxt = next_envelope(envelope, "advance", env["GITHUB_RUN_ID"])
                evidence = report["evidence"]["dispatch"] = {}
                status, _ = request(client, "POST", DISPATCH, dispatch_body(nxt), evidence)
                require(status == 204, "next seed dispatch was not HTTP 204")
                report["evidence"]["dispatched_envelope"] = nxt
                report.update(outcome="ADVANCED", all_passed=True, batch_complete=False)
    except Exception as exc:
        report.update(outcome=getattr(exc, "outcome", "INCONCLUSIVE"), all_passed=False, eligible=False, advance=False, error=str(exc))
        if isinstance(exc, lab.PermissionError): report["permission_error"] = {"status": exc.status, "body": exc.body}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        envelope = validate_envelope(lab.parse_json(os.environ.get("LAB_AB_MANIFEST", "null")))
        needs_input = args.command in ("publish", "baseline", "recover", "probe", "advance")
        require((args.input is not None) == needs_input, "--input does not match this stage")
        validate_context(envelope, os.environ, args.command)
        document = lab.parse_json(args.input.read_text(encoding="utf-8")) if needs_input else None
        client = None if args.command == "qualify" else API(args.command, envelope)
        result = execute(args.command, envelope, client, document, os.environ)
    except Exception as exc:
        result = {"schema_version": 1, "kind": "ab-" + args.command, "repository": lab.REPOSITORY,
                  "outcome": "INCONCLUSIVE", "all_passed": False, "eligible": False, "advance": False, "error": str(exc)}
        if isinstance(exc, lab.PermissionError): result["permission_error"] = {"status": exc.status, "body": exc.body}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write("eligible=" + str(result.get("eligible", False)).lower() + "\nadvance=" + str(result.get("advance", False)).lower() + "\n")
    return 0 if result.get("all_passed") is True and not (args.command == "qualify" and result.get("gate_status") == "failure") else 1


if __name__ == "__main__":
    sys.exit(main())
