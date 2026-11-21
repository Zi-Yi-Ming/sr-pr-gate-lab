"""Throwaway, isolated native Gate attribution controls; no production recovery."""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from collections import Counter
import lab
import ab_lab as ab
import native_lab as prior

REPOSITORY = lab.REPOSITORY
BASE = prior.BASE
CONTROLLER_REF = "lab/native-controls"
EXPERIMENT = "native-gate-controls-v1"
WORKFLOW = prior.WORKFLOW
PREFIX = lab.PREFIX
DISPATCH = PREFIX + "/actions/workflows/calibration.yml/dispatches"
FIXTURES = ("clean", "cancelled-no-gate")
STAGES = ("control-seed", "control-qualification", "control-gate", "control-probe")
COMMAND_STAGE = {"seed": STAGES[0], "qualify": STAGES[1], "qual-relay": STAGES[1],
                 "gate": STAGES[2], "gate-relay": STAGES[2], "probe": STAGES[3], "advance": STAGES[3]}
require = lab.require
read = ab.read
request = ab.request


def selected(m):
    return m["cases"][m["index"]]


def validate_manifest(m):
    require(type(m) is dict and type(m.get("index")) is int and m["index"] in (0, 1), "invalid controls index")
    stage = m.get("stage")
    require(stage in STAGES, "invalid controls stage")
    keys = {"schema_version", "experiment", "controller_ref", "controller_sha", "cases", "index", "stage"}
    if m["index"] == 1 and stage != "control-seed": keys.add("seed_run_id")
    if stage in ("control-gate", "control-probe"): keys.add("qualification_run_id")
    if stage == "control-probe": keys.add("gate_run_id")
    require(set(m) == keys and type(m.get("schema_version")) is int and m["schema_version"] == 1
            and m.get("experiment") == EXPERIMENT and m.get("controller_ref") == CONTROLLER_REF,
            "controls manifest identity/keys mismatch")
    require(type(m.get("controller_sha")) is str and re.fullmatch(r"[0-9a-f]{40}", m["controller_sha"]), "invalid controller SHA")
    require(type(m.get("cases")) is list and len(m["cases"]) == 2, "exactly two controls required")
    for c, f in zip(m["cases"], FIXTURES):
        require(type(c) is dict and set(c) == {"case_id", "fixture", "pr", "head", "base", "base_ref", "head_ref"}, "invalid control case keys")
        require(c["case_id"] == c["fixture"] == f and type(c["pr"]) is int and c["pr"] > 0
                and type(c["head"]) is str and re.fullmatch(r"[0-9a-f]{40}", c["head"])
                and c["base"] == BASE and c["base_ref"] == "lab-target/control-"+f
                and c["head_ref"] == "lab/control-"+f, "control case source/target mismatch")
    require(len({c["pr"] for c in m["cases"]}) == len({c["head"] for c in m["cases"]}) == 2
            and m["controller_sha"] not in {BASE, *(c["head"] for c in m["cases"])}
            and BASE not in {c["head"] for c in m["cases"]}, "control identities are not independent")
    require(not (m["index"] == 0 and stage == "control-seed"), "clean control must never seed/cancel")
    for k in keys - {"schema_version", "experiment", "controller_ref", "controller_sha", "cases", "index", "stage"}:
        require(type(m[k]) is str and re.fullmatch(r"[1-9][0-9]*", m[k]), "invalid source run ID")
    require(len([m[k] for k in keys if k.endswith("_run_id")]) == len({m[k] for k in keys if k.endswith("_run_id")}), "source run IDs reused")
    return m


def at_stage(m, stage, **ids):
    result = {k: copy.deepcopy(m[k]) for k in ("schema_version", "experiment", "controller_ref", "controller_sha", "cases", "index")}
    result["stage"] = stage
    for k in ("seed_run_id", "qualification_run_id", "gate_run_id"):
        needed = k == "seed_run_id" and m["index"] == 1 and stage != "control-seed"
        needed |= k == "qualification_run_id" and stage in ("control-gate", "control-probe")
        needed |= k == "gate_run_id" and stage == "control-probe"
        if needed: result[k] = ids.get(k, m.get(k))
    return validate_manifest(result)


def dispatch_payload(m):
    validate_manifest(m)
    return {"ref": m["controller_ref"] if m["stage"] == "control-probe" else selected(m)["head_ref"],
            "inputs": {"phase": "publish-and-probe", "manifest": json.dumps(m, separators=(",", ":")), "source_run_id": ""}}


LEGACY_NAMES = ("seed", "Native qualification", "CI Execution", "relay", "publish", "baseline", "recover", "probe", "advance",
                "supplement_baseline", "supplement_recover", "supplement_probe", "ng_seed", "Native experiment qualification",
                "Native experiment CI Execution", "ng_qual_relay", "ng_publish", "ng_baseline", "ng_native_dispatch",
                "Native Gate inactive", "ng_native_relay", "ng_probe", "ng_advance")
CONTROL_NAMES = ("cx_seed", "Control qualification", "Control CI Execution", "cx_qual_relay", "Control Gate inactive",
                 "cx_gate_relay", "cx_probe", "cx_advance")


def producer(env):
    return {k: env.get(v, "") for k, v in (("run_id", "GITHUB_RUN_ID"), ("run_attempt", "GITHUB_RUN_ATTEMPT"),
                                         ("sha", "GITHUB_SHA"), ("ref", "GITHUB_REF"))}


def validate_producer(p, m, rid):
    controller = m["stage"] == "control-probe"
    require(type(p) is dict and set(p) == {"run_id", "run_attempt", "sha", "ref"}
            and p["run_id"] == str(rid) and re.fullmatch(r"[1-9][0-9]*", p["run_id"])
            and p["run_attempt"] == "1" and p["sha"] == (m["controller_sha"] if controller else selected(m)["head"])
            and p["ref"] == "refs/heads/"+(m["controller_ref"] if controller else selected(m)["head_ref"]), "artifact producer mismatch")


def context(command, m, env):
    require(command in COMMAND_STAGE and m["stage"] == COMMAND_STAGE[command], "command/stage mismatch")
    validate_producer(producer(env), m, env.get("GITHUB_RUN_ID"))
    require(env.get("GITHUB_REPOSITORY") == REPOSITORY and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            and env.get("GITHUB_WORKFLOW_REF") == REPOSITORY+"/"+WORKFLOW+"@"+env["GITHUB_REF"], "actual workflow context mismatch")


def common(doc, kind, m, rid):
    expected = at_stage(m, COMMAND_STAGE[kind])
    require(type(doc) is dict and type(doc.get("schema_version")) is int and doc["schema_version"] == 1 and doc.get("repository") == REPOSITORY
            and doc.get("kind") == kind and doc.get("manifest") == expected and doc.get("protocol_valid") is True
            and doc.get("strict_recovery_verified") is False and "error" not in doc, "invalid "+kind+" artifact")
    validate_producer(doc.get("producer"), expected, rid)


def validate_qualification(q, m, rid=None):
    common(q, "qualify", m, rid or m["qualification_run_id"])
    require(q.get("outcome") == "QUALIFIED" and q.get("gate_success") is True and q.get("deferred") == []
            and q.get("required_contracts") == list(lab.CONTRACTS) and type(q.get("contracts")) is list
            and len(q["contracts"]) == 3, "qualification is not complete current success")
    for item, contract in zip(q["contracts"], lab.CONTRACTS):
        require(type(item) is dict and type(item.get("execution")) is dict
                and type(item["execution"].get("returncode")) is int, "qualification exit code must be an integer")
        require(item == {"id": contract, "status": "passed", "execution": {"returncode": 0, "stdout": "LAB synthetic fixture\n", "stderr": ""}},
                "failed, deferred or changed fixed-process qualification")


def qualify(report):
    report.update(required_contracts=list(lab.CONTRACTS), contracts=[], deferred=[])
    for contract in lab.CONTRACTS:
        p = subprocess.run([sys.executable, "-I", "-c", lab.PASS_CODE], capture_output=True, text=True, timeout=30, check=False, shell=False)
        report["contracts"].append(dict(id=contract, status="passed" if p.returncode == 0 else "failed",
                                        execution=dict(returncode=p.returncode, stdout=p.stdout, stderr=p.stderr)))
    report.update(gate_success=True, outcome="QUALIFIED", protocol_valid=True)
    validate_qualification(report, report["manifest"], report["producer"]["run_id"])


def bounded(body, key):
    result = ab.bounded(body, key)
    require(len(result) < 100, "saturated bounded list is not accepted")
    return result


def runs_path(m):
    return PREFIX+"/actions/workflows/calibration.yml/runs?head_sha="+selected(m)["head"]+"&event=workflow_dispatch&per_page=100"


def validate_run(r, m, rid, expected=None, complete=True):
    require(type(r) is dict and r.get("id") == int(rid) and r.get("path") == WORKFLOW and r.get("event") == "workflow_dispatch"
            and r.get("head_sha") == selected(m)["head"] and r.get("head_branch") == selected(m)["head_ref"]
            and type(r.get("run_attempt")) is int and r["run_attempt"] == 1
            and r.get("repository", {}).get("full_name") == r.get("head_repository", {}).get("full_name") == REPOSITORY
            and lab.positive(r.get("check_suite_id")), "actual run provenance mismatch")
    if complete: require(r.get("status") == "completed" and r.get("conclusion") == expected, "source run is not expected terminal result")
    else: require(r.get("status") in ("queued", "in_progress", "completed") and r.get("conclusion") in (None, "success"), "active native run failed")


def validate_jobs(jobs, r, m, role, complete=True):
    require(type(jobs) is list and 0 < len(jobs) <= 31, "job inventory outside the fixed workflow bound")
    names = list(LEGACY_NAMES+CONTROL_NAMES)
    if role == "gate": names[names.index("Control Gate inactive")] = "PR Gate"
    seen = Counter(j.get("name") for j in jobs)
    require(all(name in names and count == 1 for name, count in seen.items()), "unknown/duplicate workflow job or old same-name Gate")
    active = {"seed": {"cx_seed": "cancelled"}, "qual": {"Control qualification": "success", "Control CI Execution": "success", "cx_qual_relay": "success"},
              "gate": {"PR Gate": "success", "cx_gate_relay": "success"}}[role]
    must = set(active) if complete or role != "gate" else {"PR Gate"}
    require(must <= set(seen), "required actual workflow jobs missing")
    for j in jobs:
        require(j.get("run_id") == r["id"] and j.get("run_attempt") == 1 and j.get("head_sha") == selected(m)["head"], "job run/attempt/head mismatch")
        if complete:
            require(j.get("status") == "completed" and j.get("conclusion") == active.get(j["name"], "skipped"), "skipped Gate or unexpected source job result")
        elif j.get("status") == "completed":
            require(j.get("conclusion") == active.get(j["name"], "skipped"), "unexpected active-run failure or skipped Gate")
        prior.job_check_id(j, allow_inactive=True)


def validate_proof(e, m, gate_id, complete=True):
    expected = {"qual": m["qualification_run_id"], "gate": str(gate_id)}
    if m["index"]: expected["seed"] = m["seed_run_id"]
    require(set(e["runs"]) == set(e["jobs"]) == set(expected), "source role inventory mismatch")
    mapped = {}
    for role, rid in expected.items():
        r = e["runs"][role];done = complete or role != "gate";verdict = "cancelled" if role == "seed" else "success"
        validate_run(r, m, rid, verdict, done)
        suite = next((s for s in e["suites"] if s.get("id") == r["check_suite_id"]), None)
        ab.validate_suite(suite, r["check_suite_id"], selected(m), verdict if done else None)
        validate_jobs(e["jobs"][role], r, m, role, done)
        for j in e["jobs"][role]:
            cid = prior.job_check_id(j, allow_inactive=True)
            if cid is not None:
                require(cid not in mapped, "duplicate actual CheckRun mapping")
                mapped[cid] = (role, r, j)
    expected_suites = {r["check_suite_id"] for r in e["runs"].values()}
    require(len(expected_suites) == len(expected) and len(e["suites"]) == len(expected_suites)
            and {s.get("id") for s in e["suites"]} == expected_suites, "unexpected suite or contaminated clean control")
    require(len(e["inventory"]) == len(expected) and {r.get("id") for r in e["inventory"]} == {int(x) for x in expected.values()}, "unexpected head-bound workflow run")
    for role, rid in expected.items():
        actual = next(r for r in e["inventory"] if r.get("id") == int(rid))
        validate_run(actual, m, rid, "cancelled" if role == "seed" else "success", complete or role != "gate")
    ids = set()
    for check in e["all_checks"]:
        cid = check.get("id");require(cid in mapped and cid not in ids, "unmapped/duplicate external CheckRun")
        ids.add(cid);role, r, j = mapped[cid]
        require(check.get("name") == j["name"] and check.get("head_sha") == selected(m)["head"]
                and check.get("app", {}).get("id") == lab.APP_ID and check.get("check_suite", {}).get("id") == r["check_suite_id"], "CheckRun actual job/suite/App binding mismatch")
        if complete or role != "gate": require(check.get("status") == j["status"] and check.get("conclusion") == j["conclusion"], "CheckRun/job verdict mismatch")
    for role, active in (("qual", ("Control qualification", "Control CI Execution", "cx_qual_relay")), ("gate", ("PR Gate",))):
        for name in active:
            job = next(j for j in e["jobs"][role] if j["name"] == name)
            require(prior.job_check_id(job) in ids, "active workflow CheckRun is missing")
    gates = [x for x in e["all_checks"] if x.get("name") == "PR Gate"]
    job = next(j for j in e["jobs"]["gate"] if j["name"] == "PR Gate")
    require(len(gates) == 1 and gates[0]["id"] == prior.job_check_id(job), "old/foreign Gate contaminates control")
    require(type(gates[0].get("node_id")) is str and bool(gates[0]["node_id"]), "native CheckRun node identity missing")
    if complete: require(gates[0].get("status") == "completed" and gates[0].get("conclusion") == "success", "native Gate not successful")
    e_source = [x for x in e["all_checks"] if x["check_suite"]["id"] != e["runs"]["gate"]["check_suite_id"]]
    require(e_source == e["source_checks"], "cached source checks differ")
    return gates[0]


def fetch_proof(client, m, gate_id, complete=True):
    e = dict(runs={}, jobs={})
    expected = {"qual": m["qualification_run_id"], "gate": str(gate_id)}
    if m["index"]: expected["seed"] = m["seed_run_id"]
    for role, rid in expected.items():
        e["runs"][role] = read(client, PREFIX+"/actions/runs/"+rid)
        e["jobs"][role] = bounded(read(client, PREFIX+"/actions/runs/"+rid+"/jobs?per_page=100"), "jobs")
    e["inventory"] = bounded(read(client, runs_path(m)), "workflow_runs")
    e["suites"] = bounded(read(client, PREFIX+"/commits/"+selected(m)["head"]+"/check-suites?per_page=100"), "check_suites")
    e["all_checks"] = ab.checks(client, m)
    require(len(e["all_checks"]) < 100, "saturated check inventory")
    e["source_checks"] = [x for x in e["all_checks"] if x.get("check_suite", {}).get("id") != e["runs"]["gate"]["check_suite_id"]]
    validate_proof(e, m, gate_id, complete)
    return e


def frozen_pr(client, m):
    c = selected(m)
    value = dict(pr=read(client, PREFIX+"/pulls/"+str(c["pr"])), base_ref=read(client, PREFIX+"/git/ref/heads/"+c["base_ref"]))
    ab.validate_pr(value["pr"], c);ab.validate_target_ref(value["base_ref"], m)
    return value


def snapshot(client, m, check, merged=False):
    c = selected(m)
    s = dict(pr=read(client, PREFIX+"/pulls/"+str(c["pr"])), base_ref=read(client, PREFIX+"/git/ref/heads/"+c["base_ref"]),
             exact_check=read(client, PREFIX+"/check-runs/"+str(check["id"])), all_checks=ab.checks(client, m), latest_checks=ab.checks(client, m, "latest"), graphql={})
    request(client, "POST", "/graphql", {"query": lab.GRAPHQL_QUERY, "variables": {"node": check["node_id"], "pr": c["pr"], "head": c["head"]}}, s["graphql"])
    validate_snapshot(s, m, check, merged)
    return s


def validate_snapshot(s, m, check, merged=False):
    ab.validate_pr(s["pr"], selected(m), merged);ab.validate_target_ref(s["base_ref"], m, not merged)
    for group in ([s["exact_check"]], s["all_checks"], s["latest_checks"]):
        matches = [x for x in group if x.get("id") == check["id"]]
        require(len(matches) == 1 and ab.fingerprint(matches[0]) == ab.fingerprint(check), "current native Gate absent/changed")
    require(check.get("status") == "completed" and check.get("conclusion") == "success", "merge requires real successful native Gate")
    ab.graphql_data(s, m, check, required=not merged)


def validate_gate(g, m):
    common(g, "gate", m, m.get("gate_run_id", g.get("producer", {}).get("run_id")))
    validate_qualification(g["qualification"], m)
    require(g.get("outcome") == "CONTROL_GATE_SUCCESS" and g.get("gate_success") is True, "invalid positive Gate artifact")
    validate_proof(g["evidence"]["source"], m, g["producer"]["run_id"], False)


def validate_terminal(p, m, rid):
    common(p, "probe", m, rid);validate_gate(p["gate_document"], m)
    e = p["evidence"];check = validate_proof(e["source"], m, m["gate_run_id"])
    validate_proof(e["final_source"], m, m["gate_run_id"])
    ab.unchanged(p["gate_document"]["evidence"]["source"]["source_checks"], e["source"]["source_checks"])
    validate_snapshot(e["before"], m, check)
    ab.unchanged(e["source"]["all_checks"], e["before"]["all_checks"])
    ab.unchanged(e["source"]["all_checks"], e["final_source"]["all_checks"])
    ab.validate_pr(e["final_pr"]["pr"], selected(m));ab.validate_target_ref(e["final_pr"]["base_ref"], m)
    merged = ab.merged_response(e["merge"])
    validate_snapshot(e["after"], m, check, merged)
    ab.unchanged(e["before"]["all_checks"], e["after"]["all_checks"])
    require(p.get("advance") is True and p.get("outcome") == ("CONTROL_MERGED" if merged else "CONTROL_STILL_BLOCKED"), "invalid terminal result")
    if merged: ab.validate_merge_commit(e["after"], m, e["merge"])
    else: require(ab.gate_refusal(e["merge"]), "denial is not exact PR Gate HTTP405")


def next_dispatch(command, m, rid):
    if command == "seed": return at_stage(m, "control-qualification", seed_run_id=rid)
    if command == "qual-relay": return at_stage(m, "control-gate", qualification_run_id=rid)
    if command == "gate-relay": return at_stage(m, "control-probe", gate_run_id=rid)
    require(command == "advance" and m["index"] == 0, "no further control dispatch")
    value = {k: copy.deepcopy(m[k]) for k in ("schema_version", "experiment", "controller_ref", "controller_sha", "cases")}
    return validate_manifest(dict(value, index=1, stage="control-seed"))


def dispatch(client, command, m, rid, report):
    value = next_dispatch(command, m, rid);report["evidence"]["dispatch"] = {}
    status, _ = request(client, "POST", DISPATCH, dispatch_payload(value), report["evidence"]["dispatch"])
    require(status == 204, "dispatch failed; never retry")
    report["dispatched_manifest"] = value


def execute(command, m, client=None, document=None, env=None):
    env = os.environ if env is None else env
    report = dict(schema_version=1, repository=REPOSITORY, kind=command, manifest=copy.deepcopy(m), producer=producer(env),
                  protocol_valid=False, gate_success=False, advance=False, batch_complete=False, strict_recovery_verified=False,
                  outcome="INCONCLUSIVE", evidence={})
    try:
        validate_manifest(m);context(command, m, env)
        rid = env["GITHUB_RUN_ID"];e = report["evidence"]
        if command == "qualify": qualify(report)
        else:
            client = client or API(command, m, env)
            if command == "seed":
                e["pr"] = frozen_pr(client, m)
                runs = bounded(read(client, runs_path(m)), "workflow_runs")
                require(len(runs) == 1 and runs[0].get("id") == int(rid), "seed is not fresh")
                require(not any(x.get("name") == "PR Gate" for x in ab.checks(client, m)), "seed head has a prior required Gate")
                dispatch(client, command, m, rid, report);e["cancel"] = {}
                status, _ = request(client, "POST", PREFIX+"/actions/runs/"+rid+"/cancel", evidence=e["cancel"])
                require(status == 202, "seed cancellation not accepted");report["outcome"] = "SEED_CANCEL_ACCEPTED"
            elif command == "qual-relay":
                validate_qualification(document, m, rid);e["pr"] = frozen_pr(client, m)
                dispatch(client, command, m, rid, report);report["outcome"] = "GATE_DISPATCHED"
            elif command == "gate":
                validate_qualification(document, m);report["qualification"] = document;e["pr"] = frozen_pr(client, m)
                e["source"] = fetch_proof(client, m, rid, False)
                report.update(gate_success=True, outcome="CONTROL_GATE_SUCCESS")
            elif command == "gate-relay":
                validate_gate(document, m);e["pr"] = frozen_pr(client, m)
                e["source"] = fetch_proof(client, m, rid, False);check = validate_proof(e["source"], m, rid, False)
                require(check.get("status") == "completed" and check.get("conclusion") == "success", "actual Gate job did not complete successfully")
                ab.unchanged(document["evidence"]["source"]["source_checks"], e["source"]["source_checks"])
                dispatch(client, command, m, rid, report);report["outcome"] = "PROBE_DISPATCHED"
            elif command == "probe":
                validate_gate(document, m);report["gate_document"] = document;e["pr"] = frozen_pr(client, m)
                e["source"] = fetch_proof(client, m, m["gate_run_id"]);check = validate_proof(e["source"], m, m["gate_run_id"])
                ab.unchanged(document["evidence"]["source"]["source_checks"], e["source"]["source_checks"])
                e["before"] = snapshot(client, m, check)
                ab.unchanged(e["source"]["all_checks"], e["before"]["all_checks"])
                e["final_source"] = fetch_proof(client, m, m["gate_run_id"])
                ab.unchanged(e["source"]["all_checks"], e["final_source"]["all_checks"]);e["final_pr"] = frozen_pr(client, m)
                e["merge"] = {};request(client, "PUT", PREFIX+"/pulls/"+str(selected(m)["pr"])+"/merge", {"sha": selected(m)["head"], "merge_method": "merge"}, e["merge"])
                merged = ab.merged_response(e["merge"]);e["after"] = snapshot(client, m, check, merged)
                if merged: ab.record_merge_commit(client, m, e["merge"], e["after"]);ab.validate_merge_commit(e["after"], m, e["merge"])
                else: require(ab.gate_refusal(e["merge"]), "ordinary merge returned a different denial")
                ab.unchanged(e["before"]["all_checks"], e["after"]["all_checks"])
                report.update(advance=True, outcome="CONTROL_MERGED" if merged else "CONTROL_STILL_BLOCKED")
            elif command == "advance":
                validate_terminal(document, m, rid)
                if m["index"] == 1: report.update(batch_complete=True, outcome="BATCH_COMPLETE")
                else: dispatch(client, command, m, rid, report);report["outcome"] = "NEXT_CONTROL_DISPATCHED"
            else: require(False, "unknown command")
            report["protocol_valid"] = True
    except Exception as exc:
        report.update(protocol_valid=False, gate_success=False, advance=False, batch_complete=False, outcome="INCONCLUSIVE", error=str(exc))
    return report


class API(lab.GitHubAPI):
    _send = ab.API._send

    def __init__(self, command, m, env):
        validate_manifest(m);context(command, m, env);super().__init__(env)
        self.command, self.m, self.rid, self.writes = command, copy.deepcopy(m), env["GITHUB_RUN_ID"], set()

    def request(self, method, path, data=None):
        m = self.m;c = selected(m);allowed = False
        require(self._repository == REPOSITORY, "repository binding changed")
        if method == "GET":
            allowed = path in (PREFIX+"/pulls/"+str(c["pr"]), PREFIX+"/git/ref/heads/"+c["base_ref"], runs_path(m))
            allowed |= path in [PREFIX+"/commits/"+c["head"]+"/check-runs?filter="+f+"&per_page=100" for f in ("all", "latest")]
            allowed |= path == PREFIX+"/commits/"+c["head"]+"/check-suites?per_page=100"
            allowed |= any(re.fullmatch(re.escape(PREFIX)+s, path) for s in (r"/check-runs/[1-9][0-9]*", r"/check-suites/[1-9][0-9]*", r"/git/commits/[0-9a-f]{40}"))
            for rid in {self.rid, *(m[k] for k in m if k.endswith("_run_id"))}:
                allowed |= path in (PREFIX+"/actions/runs/"+rid, PREFIX+"/actions/runs/"+rid+"/jobs?per_page=100")
        elif method == "POST" and path == "/graphql":
            allowed = type(data) is dict and set(data) == {"query", "variables"} and data["query"] == lab.GRAPHQL_QUERY
            allowed = allowed and type(data["variables"]) is dict and set(data["variables"]) == {"node", "pr", "head"}
            allowed = allowed and data["variables"]["pr"] == c["pr"] and data["variables"]["head"] == c["head"] and type(data["variables"]["node"]) is str and bool(data["variables"]["node"])
        elif method == "POST" and path == DISPATCH:
            allowed = self.command in ("seed", "qual-relay", "gate-relay", "advance") and not (self.command == "advance" and m["index"] == 1)
            allowed = allowed and data == dispatch_payload(next_dispatch(self.command, m, self.rid))
        elif method == "POST" and path == PREFIX+"/actions/runs/"+self.rid+"/cancel":
            allowed = self.command == "seed" and ("POST", DISPATCH) in self.writes and data is None
        elif method == "PUT":
            allowed = self.command == "probe" and path == PREFIX+"/pulls/"+str(c["pr"])+"/merge" and data == {"sha": c["head"], "merge_method": "merge"}
        require(allowed, "controls operation/path/payload outside allowlist")
        if method != "GET" and path != "/graphql":
            key = method, path;require(key not in self.writes, "duplicate mutation is forbidden");self.writes.add(key)
        return self._send(method, path, data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=tuple(COMMAND_STAGE));parser.add_argument("--input");parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        m = json.loads(os.environ.get("CONTROLS_MANIFEST", "null"))
        doc = json.loads(Path(args.input).read_text(encoding="utf-8")) if args.input else None
        result = execute(args.command, m, document=doc)
    except Exception as exc:
        result = dict(protocol_valid=False, gate_success=False, advance=False, batch_complete=False, strict_recovery_verified=False, outcome="INCONCLUSIVE", error=str(exc))
    path = Path(args.output);path.parent.mkdir(parents=True, exist_ok=True);path.write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            for key in ("protocol_valid", "gate_success", "advance", "batch_complete"):stream.write(key+"="+str(result.get(key) is True).lower()+"\n")
    print(json.dumps({k: result.get(k) for k in ("protocol_valid", "outcome", "error")}))
    return 0 if result.get("protocol_valid") is True else 1


if __name__ == "__main__": sys.exit(main())
