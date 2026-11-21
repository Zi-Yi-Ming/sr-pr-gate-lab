"""Bounded native PR Gate lab protocol; no retries or production operations."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import lab
import ab_lab as ab

REPOSITORY = lab.REPOSITORY
BASE = "525bc8117c9176b158d206b324c904e18aab8534"
CONTROLLER_REF = "lab/native-controller"
EXPERIMENT = "native-pr-gate-v1"
WORKFLOW = ".github/workflows/calibration.yml"
FIXTURES = ("waiting", "partial", "failure", "success")
PREFIX = lab.PREFIX
DISPATCH = PREFIX + "/actions/workflows/calibration.yml/dispatches"
QUALIFICATION_JOB = "Native experiment qualification"
EXECUTION_JOB = "Native experiment CI Execution"
STAGES = {
    "native-seed": (), "native-qualification": ("seed_run_id",),
    "native-baseline": ("seed_run_id", "qualification_run_id"),
    "native-gate": ("seed_run_id", "qualification_run_id", "baseline_run_id", "baseline_digest"),
    "native-probe": ("seed_run_id", "qualification_run_id", "baseline_run_id", "baseline_digest", "native_run_id"),
}
COMMAND_STAGE = {"seed":"native-seed", "qualify":"native-qualification", "qual-relay":"native-qualification",
                 "publish":"native-baseline", "baseline":"native-baseline", "native-dispatch":"native-baseline",
                 "gate":"native-gate", "native-relay":"native-gate", "probe":"native-probe"}
require = lab.require
read = ab.read
request = ab.request
bounded = ab.bounded


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def selected(m):
    return m["cases"][m["index"]]


def validate_manifest(m):
    common = {"schema_version","experiment","controller_ref","controller_sha","cases","index","stage"}
    require(type(m) is dict and m.get("stage") in STAGES, "invalid native manifest/stage")
    require(set(m) == common | set(STAGES[m["stage"]]), "manifest keys do not match stage")
    require(type(m["schema_version"]) is int and m["schema_version"] == 1 and m["experiment"] == EXPERIMENT,
            "invalid native experiment/schema")
    require(m["controller_ref"] == CONTROLLER_REF and type(m["controller_sha"]) is str
            and lab.SHA_RE.fullmatch(m["controller_sha"]), "invalid frozen controller")
    require(type(m["index"]) is int and 0 <= m["index"] < 4, "finite four-case index required")
    require(type(m["cases"]) is list and len(m["cases"]) == 4, "exactly four native cases required")
    heads, prs = set(), set()
    for c,f in zip(m["cases"], FIXTURES):
        require(type(c) is dict and set(c) == {"case_id","fixture","pr","head","base","base_ref","head_ref"}, "invalid case keys")
        require(c["case_id"] == c["fixture"] == f and lab.positive(c["pr"]), "invalid case order/PR")
        require(type(c["head"]) is str and lab.SHA_RE.fullmatch(c["head"]), "invalid head SHA")
        require(c["base"] == BASE and c["base_ref"] == "lab-target/native-"+f and c["head_ref"] == "lab/native-"+f,
                "case frozen base or refs changed")
        heads.add(c["head"].lower()); prs.add(c["pr"])
    require(len(heads) == len(prs) == 4 and m["controller_sha"].lower() not in heads, "heads/PRs must be distinct from controller")
    ids = []
    for key in STAGES[m["stage"]]:
        if key == "baseline_digest":
            require(type(m[key]) is str and re.fullmatch(r"[0-9a-f]{64}",m[key]), "invalid canonical baseline digest")
        else:
            require(type(m[key]) is str and lab.POSITIVE_RE.fullmatch(m[key]), "invalid run ID")
            ids.append(m[key])
    require(len(set(ids)) == len(ids), "source runs must be distinct")
    return m


def at_stage(m, stage, **extra):
    value = {k:copy.deepcopy(m[k]) for k in ("schema_version","experiment","controller_ref","controller_sha","cases","index")}
    value["stage"] = stage
    for k in STAGES[stage]:
        if k in extra: value[k] = extra[k]
        elif k in m: value[k] = m[k]
    return validate_manifest(value)


def dispatch_payload(m):
    validate_manifest(m)
    ref = CONTROLLER_REF if m["stage"] in ("native-baseline","native-probe") else selected(m)["head_ref"]
    return {"ref":ref, "inputs":{"phase":"publish-and-probe", "manifest":json.dumps(m,separators=(",",":")), "source_run_id":""}}


def producer(env):
    return {"run_id":env.get("GITHUB_RUN_ID"),"run_attempt":env.get("GITHUB_RUN_ATTEMPT"),
            "sha":env.get("GITHUB_SHA"),"ref":env.get("GITHUB_REF")}


def expected_source(m):
    controller = m["stage"] in ("native-baseline","native-probe")
    return (m["controller_sha"],CONTROLLER_REF) if controller else (selected(m)["head"],selected(m)["head_ref"])


def validate_producer(p,m,run_id=None):
    sha,ref=expected_source(m)
    require(type(p) is dict and set(p)=={"run_id","run_attempt","sha","ref"}, "invalid producer")
    require(type(p["run_id"]) is str and lab.POSITIVE_RE.fullmatch(p["run_id"]) and p["run_attempt"]=="1"
            and p["sha"]==sha and p["ref"]=="refs/heads/"+ref, "producer context mismatch")
    require(run_id is None or p["run_id"]==run_id, "producer run mismatch")
    require(p["run_id"] not in [m[k] for k in STAGES[m["stage"]] if k.endswith("_run_id")], "producer impersonates source run")


def validate_context(command,m,env):
    validate_manifest(m)
    require(m["stage"] in ("native-baseline","native-probe") if command=="advance" else m["stage"]==COMMAND_STAGE[command], "command/stage mismatch")
    require(env.get("GITHUB_REPOSITORY")==REPOSITORY and env.get("GITHUB_EVENT_NAME")=="workflow_dispatch", "lab manual dispatch required")
    validate_producer(producer(env),m)
    require(env.get("GITHUB_WORKFLOW_REF")==REPOSITORY+"/"+WORKFLOW+"@"+env["GITHUB_REF"], "actual workflow path/ref mismatch")


def new_report(command,m,env):
    return dict(schema_version=1,kind=command,repository=REPOSITORY,manifest=copy.deepcopy(m),producer=producer(env),
                protocol_valid=False,gate_success=False,eligible=False,advance=False,batch_complete=False,
                strict_recovery_verified=False,outcome="INCONCLUSIVE",evidence={})


def common(doc,kind,m,run_id=None):
    require(type(doc) is dict and doc.get("schema_version")==1 and doc.get("kind")==kind
            and doc.get("repository")==REPOSITORY and doc.get("protocol_valid") is True
            and doc.get("strict_recovery_verified") is False and "error" not in doc, "invalid/incomplete "+kind+" evidence")
    require(doc.get("manifest")==m, "artifact manifest/source chain changed")
    validate_producer(doc.get("producer"),m,run_id)
    return doc


def qualify(report,m):
    f=selected(m)["fixture"]; contracts=[]; deferred=lab.deferred_for(f)
    for contract in lab.CONTRACTS:
        if contract in deferred: item=dict(id=contract,status="deferred",execution=None)
        else:
            code=lab.FAIL_CODE if f=="failure" and contract=="automatic" else lab.PASS_CODE
            p=subprocess.run([sys.executable,"-I","-c",code],capture_output=True,text=True,timeout=30,check=False,shell=False)
            item=dict(id=contract,status="passed" if p.returncode==0 else "failed",
                      execution=dict(returncode=p.returncode,stdout=p.stdout,stderr=p.stderr))
        contracts.append(item)
    report.update(required_contracts=list(lab.CONTRACTS),contracts=contracts,deferred=deferred,
                  gate_status=lab.derive_status(contracts),outcome="QUALIFIED")
    report["gate_success"]=report["gate_status"]=="success"


def validate_qualification(q,m,run_id=None):
    qm=at_stage(m,"native-qualification")
    common(q,"qualify",qm,run_id or m.get("qualification_run_id"))
    require(q.get("outcome")=="QUALIFIED" and q.get("required_contracts")==list(lab.CONTRACTS), "invalid qualification inventory")
    f=selected(m)["fixture"]; deferred=lab.deferred_for(f)
    require(q.get("deferred")==deferred and type(q.get("contracts")) is list and len(q["contracts"])==3, "changed deferred/inventory")
    for item,c in zip(q["contracts"],lab.CONTRACTS):
        require(type(item) is dict and set(item)=={"id","status","execution"} and item["id"]==c,"invalid contract")
        if c in deferred: require(item["status"]=="deferred" and item["execution"] is None,"deferred was promoted")
        else:
            ex=item["execution"];rc=1 if f=="failure" and c=="automatic" else 0
            require(type(ex) is dict and set(ex)=={"returncode","stdout","stderr"}
                    and type(ex["returncode"]) is int and ex["returncode"]==rc
                    and all(type(ex[k]) is str for k in ("stdout","stderr"))
                    and item["status"]==("failed" if rc else "passed"),"fixed subprocess evidence changed")
    require(q.get("gate_status")==lab.derive_status(q["contracts"]) and q.get("gate_success")== (f=="success"),"qualification was upgraded")
    return q


def validate_run(r,m,rid,*,controller=False,conclusion=None,completed=True):
    sha,ref=(m["controller_sha"],CONTROLLER_REF) if controller else (selected(m)["head"],selected(m)["head_ref"])
    require(type(r) is dict and r.get("id")==int(rid) and r.get("run_attempt")==1
            and type(r.get("run_attempt")) is int and r.get("path")==WORKFLOW and r.get("event")=="workflow_dispatch"
            and r.get("head_sha")==sha and r.get("head_branch")==ref
            and r.get("repository",{}).get("full_name")==REPOSITORY
            and r.get("head_repository",{}).get("full_name")==REPOSITORY and lab.positive(r.get("check_suite_id")), "real workflow run provenance mismatch")
    if completed: require(r.get("status")=="completed" and r.get("conclusion")==conclusion,"run conclusion changed or incomplete")
    else: require(r.get("status") in ("queued","in_progress","completed"),"invalid active workflow run status")


def runs_path(m):
    return PREFIX+"/actions/workflows/calibration.yml/runs?head_sha="+selected(m)["head"]+"&event=workflow_dispatch&per_page=100"


def validate_source_proof(proof,m,native_id=None):
    c=selected(m);expected="failure" if c["fixture"]=="failure" else "success"
    for key,rid,conclusion in (("seed",m["seed_run_id"],"cancelled"),("qualification",m["qualification_run_id"],expected)):
        r=proof[key+"_run"];validate_run(r,m,rid,conclusion=conclusion)
        ab.validate_suite(proof[key+"_suite"],r["check_suite_id"],c,conclusion)
    require(proof["seed_run"]["check_suite_id"]!=proof["qualification_run"]["check_suite_id"],"source suites alias")
    jobs=proof["qualification_jobs"]
    for name in (QUALIFICATION_JOB,EXECUTION_JOB,"ng_qual_relay"):
        matches=[j for j in jobs if j.get("name")==name]
        want="success" if name=="ng_qual_relay" else expected
        require(len(matches)==1 and matches[0].get("status")=="completed" and matches[0].get("conclusion")==want,"expected source job absent/failed/skipped")
    for j in jobs:
        require(j.get("run_id")==int(m["qualification_run_id"]) and j.get("run_attempt")==1
                and j.get("head_sha")==c["head"],"qualification job run/attempt/head mismatch")
        job_check_id(j,allow_inactive=True)
        allowed={"success","skipped"}
        if j.get("name") in (QUALIFICATION_JOB,EXECUTION_JOB) and expected=="failure":allowed.add("failure")
        require(j.get("status")=="completed" and j.get("conclusion") in allowed,"unexpected source job failure")
        require(j.get("name")!="PR Gate","native Gate name leaked outside native stage")
    wanted={int(m["seed_run_id"]),int(m["qualification_run_id"])} | ({int(native_id)} if native_id else set())
    inventory=proof["workflow_runs"]
    require(len(inventory)==len(wanted) and {r.get("id") for r in inventory}==wanted,"head workflow inventory duplicated/missing")
    for r in inventory:
        require(r.get("head_sha")==c["head"] and r.get("run_attempt")==1,"inventory source mismatch")
        if r["id"] in (int(m["seed_run_id"]),int(m["qualification_run_id"])):
            key="seed_run" if r["id"]==int(m["seed_run_id"]) else "qualification_run"
            require(r==proof[key],"source inventory/read disagrees")
        else:
            expected="success" if c["fixture"]=="success" else "failure"
            validate_run(r,m,native_id,conclusion=expected,completed=r.get("status")=="completed")


def fetch_sources(client,m,target,native_id=None):
    for key in ("seed","qualification"):
        target[key+"_run"]=read(client,PREFIX+"/actions/runs/"+m[key+"_run_id"])
        target[key+"_suite"]=read(client,PREFIX+"/check-suites/"+str(target[key+"_run"]["check_suite_id"]))
    target["qualification_jobs"]=bounded(read(client,PREFIX+"/actions/runs/"+m["qualification_run_id"]+"/jobs?per_page=100"),"jobs")
    target["workflow_runs"]=bounded(read(client,runs_path(m)),"workflow_runs")
    validate_source_proof(target,m,native_id)


def external(m,stale=False):
    return EXPERIMENT+(":stale:" if stale else ":current:")+selected(m)["case_id"]+":"+m["qualification_run_id"]


def check_payload(m,stale=False):
    status,conclusion=("completed","success") if stale else ab.fixture_state(selected(m)["fixture"])
    p=dict(name="PR Gate",head_sha=selected(m)["head"],external_id=external(m,stale),status=status,
           output=dict(title="Native lab synthetic "+("stale fixture" if stale else "current qualification"),
                       summary="Synthetic platform experiment; not production authorization or recovery evidence."))
    if conclusion:p["conclusion"]=conclusion
    return p


def validate_check(c,m,stale=False,receipt=None):
    p=check_payload(m,stale)
    require(type(c) is dict and all(c.get(k)==v for k,v in p.items() if k!="output")
            and c.get("conclusion")==p.get("conclusion"),"current/stale Gate identity/state changed")
    require(lab.positive(c.get("id")) and type(c.get("node_id")) is str and c["node_id"]
            and c.get("app",{}).get("id")==lab.APP_ID and lab.positive(c.get("check_suite",{}).get("id")),"Gate App/ID/suite invalid")
    if receipt: require(ab.fingerprint(c)==ab.fingerprint(receipt),"Gate receipt changed")


def validate_publication(p,m,run_id=None):
    pm=at_stage(m,"native-baseline");common(p,"publish",pm,run_id)
    validate_qualification(p["qualification"],m);validate_source_proof(p["source"],m)
    require(p["outcome"]=="PUBLISHED","publication incomplete")
    validate_check(p["current_check"],m)
    stale=p["stale_check"]
    if selected(m)["fixture"]=="success": require(stale is None,"success has stale green")
    else:validate_check(stale,m,True)
    added=[p["current_check"]["id"]]+([stale["id"]] if stale else [])
    ab.unchanged(p["before_checks"],p["after_checks"],added=added)
    byid={c["id"]:c for c in p["after_checks"]}
    validate_check(byid.get(p["current_check"]["id"]),m,receipt=p["current_check"])
    if stale:validate_check(byid.get(stale["id"]),m,True,stale)
    receipts=p["evidence"].get("receipts")
    expected_checks=([stale] if stale else [])+[p["current_check"]]
    require(type(receipts) is list and len(receipts)==len(expected_checks),"missing publication POST receipts")
    for receipt,check in zip(receipts,expected_checks):
        require(receipt.get("status")==201 and receipt.get("body")==check,"publication POST receipt changed")
    return p


def current_snapshot(client,m,current,target,*,merged=False):
    c=selected(m)
    target["pr"]=read(client,PREFIX+"/pulls/"+str(c["pr"]));ab.validate_pr(target["pr"],c,merged)
    target["base_ref"]=read(client,PREFIX+"/git/ref/heads/"+c["base_ref"]);ab.validate_target_ref(target["base_ref"],m,not merged)
    target["exact_check"]=read(client,PREFIX+"/check-runs/"+str(current["id"]))
    require(ab.fingerprint(target["exact_check"])==ab.fingerprint(current),"authoritative CheckRun changed")
    for f in ("all","latest"):
        checks=ab.checks(client,m,f);target[f+"_checks"]=checks
        matches=[x for x in checks if x.get("id")==current["id"]]
        require(len(matches)==1 and ab.fingerprint(matches[0])==ab.fingerprint(current),"authoritative Gate missing/changed in "+f)
    target["graphql"]={}
    request(client,"POST","/graphql",dict(query=lab.GRAPHQL_QUERY,variables=dict(node=current["node_id"],pr=c["pr"],head=c["head"])),target["graphql"])
    ab.graphql_data(target,m,current,required=not merged)
    target["gate_suite"]=read(client,PREFIX+"/check-suites/"+str(current["check_suite"]["id"]))
    ab.validate_suite(target["gate_suite"],current["check_suite"]["id"],c)
    # Null rollup remains an observation, never strict recovery evidence.


def baseline_topology(p,m,snapshot):
    validate_check(snapshot["exact_check"],m,receipt=p["current_check"])
    if selected(m)["fixture"]=="success":
        require(p["current_check"]["check_suite"]["id"]==p["source"]["seed_run"]["check_suite_id"],"old green Gate not in cancelled seed suite")
        ab.validate_suite(snapshot["gate_suite"],p["source"]["seed_run"]["check_suite_id"],selected(m),"cancelled")


def validate_snapshot(s,m,c,before,*,merged=False):
    ab.validate_pr(s["pr"],selected(m),merged);ab.validate_target_ref(s["base_ref"],m,not merged)
    require(ab.fingerprint(s["exact_check"])==ab.fingerprint(c),"snapshot exact check mismatch")
    for f in ("all","latest"):
        matches=[x for x in s[f+"_checks"] if x.get("id")==c["id"]]
        require(len(matches)==1 and ab.fingerprint(matches[0])==ab.fingerprint(c),"snapshot authoritative Gate absent")
    ab.graphql_data(s,m,c,required=not merged);ab.unchanged(before,s["all_checks"])
    ab.validate_suite(s["gate_suite"],c["check_suite"]["id"],selected(m))


def validate_baseline(b,m,run_id=None,eligible=True):
    bm=at_stage(m,"native-baseline")
    common(b,"baseline",bm,run_id or m.get("baseline_run_id"))
    p=validate_publication(b["publication"],m,b["producer"]["run_id"])
    require(b["publication"]["producer"]==b["producer"],"publication must be same actual baseline run")
    validate_source_proof(b["evidence"]["source"],m)
    before,after=b["evidence"]["before"],b["evidence"]["after"]
    validate_snapshot(before,m,p["current_check"],p["after_checks"]);baseline_topology(p,m,before)
    if eligible:
        require(b["eligible"] is True and b["advance"] is False and b["outcome"]=="ELIGIBLE_NATIVE_PROBE_ONLY"
                and ab.gate_refusal(b["evidence"]["merge"]),"baseline does not authorize native dispatch")
        validate_snapshot(after,m,p["current_check"],before["all_checks"])
        ab.negative_safe(after,selected(m))
    else:
        require(selected(m)["fixture"]=="success" and b["outcome"]=="BASELINE_MERGED_NO_RECOVERY_NEEDED"
                and b["advance"] is True and b["eligible"] is False and ab.merged_response(b["evidence"]["merge"]),"invalid baseline terminal proof")
        validate_snapshot(after,m,p["current_check"],before["all_checks"],merged=True)
        ab.validate_merge_commit(after,m,b["evidence"]["merge"])
    if "baseline_digest" in m:require(digest(b)==m["baseline_digest"],"pinned baseline digest mismatch")
    return p


def job_check_id(job,*,allow_inactive=False):
    inactive=job.get("status") in ("queued","waiting") or (job.get("status")=="completed" and job.get("conclusion")=="skipped")
    if allow_inactive and job.get("name")!="PR Gate" and inactive and job.get("check_run_url") is None:
        return None
    match=re.fullmatch(r"https://api\.github\.com"+re.escape(PREFIX)+r"/check-runs/([1-9][0-9]*)",job.get("check_run_url") or "")
    require(match is not None,"job check_run_url not an exact lab API CheckRun URL")
    return int(match.group(1))


def validate_native_jobs(target,m,rid,*,complete=False,gate_complete=False):
    r=target["run"];expected="success" if selected(m)["fixture"]=="success" else "failure"
    validate_run(r,m,rid,conclusion=expected,completed=complete)
    ab.validate_suite(target["suite"],r["check_suite_id"],selected(m),expected if complete else None)
    jobs=target["jobs"];ids=[check_id for j in jobs if (check_id:=job_check_id(j,allow_inactive=True)) is not None]
    require(len(ids)==len(set(ids)),"duplicate job CheckRun mapping")
    gates=[j for j in jobs if j.get("name")=="PR Gate"]
    require(len(gates)==1,"exactly one actual native PR Gate job required")
    if complete:
        relays=[j for j in jobs if j.get("name")=="ng_native_relay"]
        require(len(relays)==1 and relays[0].get("status")=="completed" and relays[0].get("conclusion")=="success",
                "native probe requires successful actual relay")
    for j in jobs:
        require(j.get("run_id")==int(rid) and j.get("run_attempt")==1 and j.get("head_sha")==selected(m)["head"],"native job run/attempt/head mismatch")
        if complete or j.get("name")=="PR Gate" and gate_complete:
            require(j.get("status")=="completed","native job incomplete")
            allowed={expected} if j.get("name")=="PR Gate" else {"success","skipped"}
            require(j.get("conclusion") in allowed,"native job skipped or unexpected failure")
        elif j.get("status")=="completed":
            require(j.get("conclusion") in ({expected} if j.get("name")=="PR Gate" else {"success","skipped"}),"unexpected native job failure")
    check=target["check"]
    require(check.get("id")==job_check_id(gates[0]) and check.get("name")=="PR Gate"
            and check.get("head_sha")==selected(m)["head"] and check.get("app",{}).get("id")==lab.APP_ID
            and check.get("check_suite",{}).get("id")==r["check_suite_id"] and type(check.get("node_id")) is str and check["node_id"],"native CheckRun is not owned by actual Gate job/suite/App")
    if complete or gate_complete:
        require(check.get("status")=="completed" and check.get("conclusion")==expected,"native CheckRun verdict mismatch")
    return check


def native_jobs(client,m,rid,target,*,complete=False,gate_complete=False):
    target["run"]=read(client,PREFIX+"/actions/runs/"+rid)
    target["suite"]=read(client,PREFIX+"/check-suites/"+str(target["run"]["check_suite_id"]))
    target["jobs"]=bounded(read(client,PREFIX+"/actions/runs/"+rid+"/jobs?per_page=100"),"jobs")
    gates=[j for j in target["jobs"] if j.get("name")=="PR Gate"]
    require(len(gates)==1,"exactly one actual native PR Gate job required")
    target["check"]=read(client,PREFIX+"/check-runs/"+str(job_check_id(gates[0])))
    return validate_native_jobs(target,m,rid,complete=complete,gate_complete=gate_complete)


def validate_preservation(before,current,m,native=None):
    oldids={c["id"] for c in before};extra=[c for c in current if c["id"] not in oldids]
    if native:
        jobs={check_id:j for j in native["jobs"] if (check_id:=job_check_id(j,allow_inactive=True)) is not None}
        suite=native["run"]["check_suite_id"]
        for check in extra:
            j=jobs.get(check["id"])
            require(j is not None and check.get("name")==j.get("name") and check.get("head_sha")==selected(m)["head"]
                    and check.get("app",{}).get("id")==lab.APP_ID and check.get("check_suite",{}).get("id")==suite,
                    "added check not an actual native workflow job")
    else:require(not extra,"unexpected checks before native dispatch")
    ab.unchanged(before,current,added=[c["id"] for c in extra])


def fresh_baseline(client,m,b,target,native_id=None,*,complete=False,gate_complete=False):
    p=validate_baseline(b,m)
    target["source"]={};fetch_sources(client,m,target["source"],native_id)
    if native_id:
        controller=read(client,PREFIX+"/actions/runs/"+b["producer"]["run_id"]);target["baseline_run"]=controller
        validate_run(controller,m,b["producer"]["run_id"],controller=True,conclusion="success")
        target["native"]={};native_jobs(client,m,native_id,target["native"],complete=complete,gate_complete=gate_complete)
    target["all_checks"]=ab.checks(client,m)
    validate_preservation(b["evidence"]["after"]["all_checks"],target["all_checks"],m,target.get("native"))
    # The old API Gate remains unchanged, but native execution now owns latest.
    old=read(client,PREFIX+"/check-runs/"+str(p["current_check"]["id"]));validate_check(old,m,receipt=p["current_check"])
    target["current"]={}
    current=target["native"]["check"] if native_id else p["current_check"]
    current_snapshot(client,m,current,target["current"])
    if not native_id:baseline_topology(p,m,target["current"])
    return p


def validate_native_report(n,m,b):
    nm=at_stage(m,"native-gate")
    common(n,"gate",nm,m.get("native_run_id"))
    validate_baseline(b,nm)
    require(n.get("baseline")==b and n.get("baseline_digest")==digest(b),"native artifact lost original baseline")
    success=selected(m)["fixture"]=="success"
    require(n.get("gate_success") is success and n.get("outcome")==("NATIVE_SUCCESS" if success else "NATIVE_FAIL_CLOSED"),"native verdict was promoted")
    e=n["evidence"];rid=n["producer"]["run_id"]
    validate_source_proof(e["source"],m,rid)
    validate_run(e["baseline_run"],m,b["producer"]["run_id"],controller=True,conclusion="success")
    validate_native_jobs(e["native"],m,rid)
    validate_preservation(b["evidence"]["after"]["all_checks"],e["all_checks"],m,e["native"])
    validate_snapshot(e["current"],m,e["native"]["check"],e["all_checks"])
    return n


def merge_probe(client,m,report,current,before_checks,*,native=False):
    e=report["evidence"];e["merge"]={}
    request(client,"PUT",PREFIX+"/pulls/"+str(selected(m)["pr"])+"/merge",
            {"sha":selected(m)["head"],"merge_method":"merge"},e["merge"])
    positive=selected(m)["fixture"]=="success";merged=ab.merged_response(e["merge"])
    if not positive:
        e["merge_pr_readback"]=read(client,PREFIX+"/pulls/"+str(selected(m)["pr"]))
        if merged or e["merge_pr_readback"].get("merged") is True or e["merge_pr_readback"].get("merged_at"):
            raise lab.UnsafeError("negative fixture merged")
    e["after"]={};current_snapshot(client,m,current,e["after"],merged=merged)
    if native:
        ab.validate_suite(e["after"]["gate_suite"],current["check_suite"]["id"],selected(m),"success" if positive else "failure")
    ab.unchanged(before_checks,e["after"]["all_checks"])
    if not positive:ab.negative_safe(e["after"],selected(m))
    if merged:
        ab.record_merge_commit(client,m,e["merge"],e["after"]);ab.validate_merge_commit(e["after"],m,e["merge"])
        report.update(outcome="MERGED_NATIVE_PROBE_ONLY" if native else "BASELINE_MERGED_NO_RECOVERY_NEEDED",advance=True)
    else:
        require(ab.gate_refusal(e["merge"]),"merge refusal is not exact PR Gate HTTP405")
        ab.negative_safe(e["after"],selected(m))
        if native:report.update(outcome="STILL_BLOCKED_NATIVE_PROBE_ONLY" if positive else "NEGATIVE_NATIVE_REFUSED",advance=True)
        else:report.update(outcome="ELIGIBLE_NATIVE_PROBE_ONLY",eligible=True)


def dispatch(client,m,report):
    report["evidence"]["dispatch"]={}
    status,_=request(client,"POST",DISPATCH,dispatch_payload(m),report["evidence"]["dispatch"])
    require(status==204,"dispatch was not accepted; no retry")
    report["dispatched_manifest"]=m


def execute(command,m,client=None,document=None,native_document=None,env=None):
    env=os.environ if env is None else env
    report=new_report(command,m,env)
    try:
        validate_context(command,m,env)
        rid=env["GITHUB_RUN_ID"];c=selected(m);e=report["evidence"]
        if command=="qualify":qualify(report,m)
        else:
            client=client if client is not None else API(command,m,env)
            if command=="seed":
                e["before_checks"]=ab.checks(client,m)
                require(not any(x.get("name")=="PR Gate" for x in e["before_checks"]),"prior Gates on new native head")
                runs=bounded(read(client,runs_path(m)),"workflow_runs");e["runs"]=runs
                require(len(runs)==1 and runs[0].get("id")==int(rid),"seed head has prior/duplicate workflow runs")
                validate_run(runs[0],m,rid,completed=False)
                dispatch(client,at_stage(m,"native-qualification",seed_run_id=rid),report)
                e["cancel"]={};status,_=request(client,"POST",PREFIX+"/actions/runs/"+rid+"/cancel",evidence=e["cancel"])
                require(status==202,"seed cancellation not accepted");report["outcome"]="SEED_CANCEL_ACCEPTED"
            elif command=="qual-relay":
                validate_qualification(document,m,rid)
                report["qualification"]=document
                dispatch(client,at_stage(m,"native-baseline",qualification_run_id=rid),report);report["outcome"]="BASELINE_DISPATCHED"
            elif command=="publish":
                validate_qualification(document,m);report["qualification"]=document
                report["source"]={};fetch_sources(client,m,report["source"])
                pr=read(client,PREFIX+"/pulls/"+str(c["pr"]));ab.validate_pr(pr,c)
                ab.validate_target_ref(read(client,PREFIX+"/git/ref/heads/"+c["base_ref"]),m)
                report["before_checks"]=ab.checks(client,m)
                require(not any(x.get("name")=="PR Gate" for x in report["before_checks"]),"prior Gate prevents duplicate publication")
                report["stale_check"]=None;e["receipts"]=[]
                for stale in ([True,False] if c["fixture"]!="success" else [False]):
                    receipt={};e["receipts"].append(receipt)
                    status,check=request(client,"POST",PREFIX+"/check-runs",check_payload(m,stale),receipt)
                    require(status==201,"Gate POST not created");validate_check(check,m,stale)
                    report["stale_check" if stale else "current_check"]=check
                report["after_checks"]=ab.checks(client,m);report["outcome"]="PUBLISHED"
                report["protocol_valid"]=True;validate_publication(report,m,rid)
            elif command=="baseline":
                p=validate_publication(document,m,rid);report["publication"]=p
                e["source"]={};fetch_sources(client,m,e["source"])
                e["before"]={};current_snapshot(client,m,p["current_check"],e["before"])
                ab.unchanged(p["after_checks"],e["before"]["all_checks"]);baseline_topology(p,m,e["before"])
                # Revalidate actual qualification immediately before the one merge PUT.
                e["source"]={};fetch_sources(client,m,e["source"])
                merge_probe(client,m,report,p["current_check"],e["before"]["all_checks"])
            elif command=="native-dispatch":
                validate_baseline(document,m,rid);report["baseline"]=document
                fresh_baseline(client,m,document,e)
                if hasattr(client,"authorize_baseline"):client.authorize_baseline(document)
                dispatch(client,at_stage(m,"native-gate",baseline_run_id=rid,baseline_digest=digest(document)),report)
                report["outcome"]="NATIVE_DISPATCHED"
            elif command=="gate":
                fresh_baseline(client,m,document,e,rid)
                report.update(baseline=document,baseline_digest=digest(document),gate_success=c["fixture"]=="success",
                              outcome="NATIVE_SUCCESS" if c["fixture"]=="success" else "NATIVE_FAIL_CLOSED")
            elif command=="native-relay":
                validate_native_report(native_document,m,document)
                require(native_document["producer"]==producer(env),"native relay must consume same-run verdict")
                fresh_baseline(client,m,document,e,rid,gate_complete=True)
                report.update(baseline=document,native=native_document)
                dispatch(client,at_stage(m,"native-probe",native_run_id=rid),report);report["outcome"]="PROBE_DISPATCHED"
            elif command=="probe":
                validate_native_report(native_document,m,document)
                fresh_baseline(client,m,document,e,m["native_run_id"],complete=True)
                current=e["native"]["check"];e["native_check"]=current
                require(current["check_suite"]["id"] not in (e["source"]["seed_run"]["check_suite_id"],e["source"]["qualification_run"]["check_suite_id"]),"native suite aliases earlier suite")
                report.update(baseline=document,native=native_document)
                e["before"]={};current_snapshot(client,m,current,e["before"])
                ab.unchanged(e["all_checks"],e["before"]["all_checks"])
                e["final_source"]={};fetch_sources(client,m,e["final_source"],m["native_run_id"])
                e["final_native"]={};native_jobs(client,m,m["native_run_id"],e["final_native"],complete=True)
                require(ab.fingerprint(e["final_native"]["check"])==ab.fingerprint(current),"native verdict changed before PUT")
                merge_probe(client,m,report,current,e["before"]["all_checks"],native=True)
            elif command=="advance":
                validate_advance(document,m,rid)
                report["source"]=document
                if m["index"]==3:report.update(batch_complete=True,outcome="BATCH_COMPLETE")
                else:
                    next_m=at_stage(m,"native-seed");next_m["index"]+=1
                    dispatch(client,next_m,report);report["outcome"]="NEXT_SEED_DISPATCHED"
        report["protocol_valid"]=True
    except Exception as exc:
        report.update(protocol_valid=False,gate_success=False,eligible=False,advance=False,batch_complete=False,
                      outcome=getattr(exc,"outcome","INCONCLUSIVE"),error=str(exc))
        if isinstance(exc,lab.PermissionError):report["permission_error"]={"status":exc.status,"body":exc.body}
    return report


def validate_advance(p,m,rid):
    if m["stage"]=="native-baseline":
        validate_baseline(p,m,rid,eligible=False);require(m["index"]==3,"only last success baseline can end batch");return
    common(p,"probe",m,rid)
    b=p["baseline"];validate_native_report(p["native"],m,b)
    e=p["evidence"];validate_source_proof(e["source"],m,m["native_run_id"]);validate_source_proof(e["final_source"],m,m["native_run_id"])
    c=e["native_check"];success=selected(m)["fixture"]=="success"
    for key in ("native","final_native"):
        validate_native_jobs(e[key],m,m["native_run_id"],complete=True)
        require(ab.fingerprint(e[key]["check"])==ab.fingerprint(c),"advance native check changed")
    validate_preservation(b["evidence"]["after"]["all_checks"],e["all_checks"],m,e["native"])
    require(p["advance"] is True and p["eligible"] is False,"probe cannot advance")
    merged=ab.merged_response(e["merge"])
    want="MERGED_NATIVE_PROBE_ONLY" if merged and success else "STILL_BLOCKED_NATIVE_PROBE_ONLY" if success else "NEGATIVE_NATIVE_REFUSED"
    require(p["outcome"]==want,"invalid terminal outcome")
    validate_snapshot(e["before"],m,c,e["all_checks"])
    validate_snapshot(e["after"],m,c,e["before"]["all_checks"],merged=merged)
    if merged:
        require(success,"unsafe negative merge cannot advance");ab.validate_merge_commit(e["after"],m,e["merge"])
    else:
        require(ab.gate_refusal(e["merge"]),"advance requires exact Gate405");ab.negative_safe(e["after"],selected(m))


class API(lab.GitHubAPI):
    """Operation-specific allowlist; no CheckRun mutations in any native job."""
    _send = ab.API._send
    def __init__(self,command,m,env):
        validate_context(command,m,env);super().__init__(env)
        self.command=command;self.manifest=copy.deepcopy(m);self.run_id=env["GITHUB_RUN_ID"]
        self.writes=set();self.dispatched=False;self.baseline_digest=None
    def authorize_baseline(self,baseline):
        require(self.command=="native-dispatch","only native-dispatch authorizes a baseline digest")
        validate_baseline(baseline,self.manifest,self.run_id)
        self.baseline_digest=digest(baseline)
    def request(self,method,path,data=None):
        m=self.manifest;c=selected(m);allowed=False
        require(self._repository==REPOSITORY,"repository binding changed")
        if method=="GET":
            patterns=(r"/check-runs/[1-9][0-9]*",r"/check-suites/[1-9][0-9]*",r"/git/commits/[0-9a-fA-F]{40}")
            allowed=any(re.fullmatch(re.escape(PREFIX)+p,path) for p in patterns)
            allowed |= path in (PREFIX+"/pulls/"+str(c["pr"]),PREFIX+"/git/ref/heads/"+c["base_ref"],runs_path(m))
            allowed |= path in [PREFIX+"/commits/"+c["head"]+"/check-runs?filter="+f+"&per_page=100" for f in ("all","latest")]
            for rid in [self.run_id]+[m[k] for k in STAGES[m["stage"]] if k.endswith("_run_id")]:
                allowed |= path in (PREFIX+"/actions/runs/"+rid,PREFIX+"/actions/runs/"+rid+"/jobs?per_page=100")
        elif method=="POST" and path=="/graphql":
            allowed=(type(data) is dict and set(data)=={"query","variables"} and data["query"]==lab.GRAPHQL_QUERY
                     and type(data["variables"]) is dict and set(data["variables"])=={"node","pr","head"}
                     and type(data["variables"]["node"]) is str and bool(data["variables"]["node"])
                     and data["variables"]["pr"]==c["pr"] and data["variables"]["head"]==c["head"])
        elif method=="POST" and path==PREFIX+"/check-runs":
            allowed=self.command=="publish" and data in ([check_payload(m),check_payload(m,True)] if c["fixture"]!="success" else [check_payload(m)])
        elif method=="PUT":
            allowed=self.command in ("baseline","probe") and path==PREFIX+"/pulls/"+str(c["pr"])+"/merge" and data=={"sha":c["head"],"merge_method":"merge"}
        elif method=="POST" and path==DISPATCH and not self.dispatched:
            expected=None
            if self.command=="seed":expected=at_stage(m,"native-qualification",seed_run_id=self.run_id)
            elif self.command=="qual-relay":expected=at_stage(m,"native-baseline",qualification_run_id=self.run_id)
            elif self.command=="native-relay":expected=at_stage(m,"native-probe",native_run_id=self.run_id)
            elif self.command=="advance" and m["index"]<3:
                expected=at_stage(m,"native-seed");expected["index"]+=1
            elif self.command=="native-dispatch" and self.baseline_digest is not None:
                expected=at_stage(m,"native-gate",baseline_run_id=self.run_id,baseline_digest=self.baseline_digest)
            allowed=expected is not None and data==dispatch_payload(expected)
        elif method=="POST" and path==PREFIX+"/actions/runs/"+self.run_id+"/cancel":
            allowed=self.command=="seed" and self.dispatched and data is None
        require(allowed,"native API operation/path/payload outside allowlist")
        if method!="GET" and path!="/graphql":
            key=(method,path,data.get("external_id") if type(data) is dict else None)
            require(key not in self.writes,"duplicate mutation forbidden")
            self.writes.add(key)
        result=self._send(method,path,data)
        if path==DISPATCH and result[0]==204:self.dispatched=True
        return result


def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument("command",choices=list(COMMAND_STAGE)+["advance"])
    parser.add_argument("--output",required=True);parser.add_argument("--input");parser.add_argument("--native-input")
    args=parser.parse_args(argv)
    try:
        m=lab.parse_json(os.environ.get("NATIVE_MANIFEST","null"))
        document=lab.parse_json(Path(args.input).read_text(encoding="utf-8")) if args.input else None
        native=lab.parse_json(Path(args.native_input).read_text(encoding="utf-8")) if args.native_input else None
        report=execute(args.command,m,document=document,native_document=native)
    except Exception as exc:
        report=new_report(args.command,None,os.environ);report["error"]=str(exc)
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"],"a",encoding="utf-8") as stream:
            for key in ("protocol_valid","gate_success","eligible","advance","batch_complete"):
                stream.write(key+"="+str(report[key]).lower()+"\n")
    failed=not report["protocol_valid"] or args.command=="gate" and not report["gate_success"]
    failed |= args.command=="qualify" and report.get("gate_status")=="failure"
    return 1 if failed else 0


if __name__=="__main__":sys.exit(main())
