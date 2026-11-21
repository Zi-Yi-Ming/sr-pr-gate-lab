"""Native platform protocol: real subprocesses and controlled HTTP boundary."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lab
import native_lab as ng
from test_ab_lab import FakeAPI as OldFake, REPO, P, BASE, CONTROLLER

CASES = [{"case_id": f, "fixture": f, "pr": 30+i, "head": format(i+20, "040x"), "base": BASE,
          "base_ref": "lab-target/native-"+f, "head_ref": "lab/native-"+f}
         for i, f in enumerate(("waiting", "partial", "failure", "success"))]

def manifest(i=0, stage="native-baseline", baseline=None):
    m = dict(schema_version=1, experiment="native-pr-gate-v1", controller_ref="lab/native-controller",
             controller_sha=CONTROLLER, cases=copy.deepcopy(CASES), index=i, stage=stage)
    if stage != "native-seed": m["seed_run_id"] = "100"
    if stage not in ("native-seed", "native-qualification"): m["qualification_run_id"] = "200"
    if stage in ("native-gate", "native-probe"):
        m.update(baseline_run_id="300", baseline_digest=ng.digest(baseline))
    if stage == "native-probe": m["native_run_id"] = "400"
    return m

def environment(m):
    controller = m["stage"] in ("native-baseline", "native-probe")
    ref = m["controller_ref"] if controller else CASES[m["index"]]["head_ref"]
    return dict(GITHUB_REPOSITORY=REPO, GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_RUN_ATTEMPT="1",
                GITHUB_RUN_ID={"native-seed":"100", "native-qualification":"200", "native-baseline":"300",
                               "native-gate":"400", "native-probe":"500"}[m["stage"]],
                GITHUB_SHA=CONTROLLER if controller else CASES[m["index"]]["head"],
                GITHUB_REF="refs/heads/"+ref, GITHUB_WORKFLOW_REF=REPO+"/.github/workflows/calibration.yml@refs/heads/"+ref,
                NATIVE_MANIFEST=json.dumps(m))

def run(command, i=0, api=None, doc=None, native=None, stage=None):
    stage = stage or {"seed":"native-seed", "qualify":"native-qualification", "qual-relay":"native-qualification",
                      "gate":"native-gate", "native-relay":"native-gate", "probe":"native-probe"}.get(command, "native-baseline")
    m = manifest(i, stage, doc if stage in ("native-gate", "native-probe") else None)
    return ng.execute(command, m, client=api, document=doc, native_document=native, env=environment(m))

class FakeAPI(OldFake):
    def __init__(self, i=0):
        super().__init__(i)
        self.case = copy.deepcopy(CASES[i]); self.merge_parents = [BASE, self.case["head"]]
        for r in self.runs.values(): r.update(head_branch=self.case["head_ref"], head_sha=self.case["head"])
        for s in self.suites.values(): s["head_sha"] = self.case["head"]
        for c in self.checks.values(): c["head_sha"] = self.case["head"]
        self.jobs[0]["name"] = "Native experiment CI Execution"
        self.jobs[1]["name"] = "Native experiment qualification"
        self.jobs[2]["name"] = "ng_qual_relay"
        for idx,j in enumerate(self.jobs):
            j.update(run_id=200,run_attempt=1,head_sha=self.case["head"],check_run_url="https://api.github.com"+P+"/check-runs/"+str(12+idx))
            self.checks[12+idx]=self.make_check(12+idx,502,j["name"],"completed",j["conclusion"],None)
        self.controller_runs = {}
        for rid in (300, 500):
            self.controller_runs[rid] = dict(self.runs[200], id=rid, head_branch="lab/native-controller", head_sha=CONTROLLER,
                                             check_suite_id=rid+1000, status="in_progress", conclusion=None)
        self.native_jobs = []
        self.null_rollup = True
    def start_native(self, completed=False):
        verdict = "success" if self.case["fixture"] == "success" else "failure"
        self.controller_runs[300].update(status="completed", conclusion="success")
        self.runs[400] = dict(self.runs[200], id=400, check_suite_id=503,
                              status="completed" if completed else "in_progress", conclusion=verdict if completed else None)
        self.suites[503].update(status="completed" if completed else "in_progress", conclusion=verdict if completed else None)
        self.native_jobs = [dict(id=90001, run_id=400, run_attempt=1, head_sha=self.case["head"], name="PR Gate",
                            check_run_url="https://api.github.com"+P+"/check-runs/8001", status="completed" if completed else "in_progress",
                            conclusion=verdict if completed else None),
                            dict(id=90002, run_id=400, run_attempt=1, head_sha=self.case["head"], name="ng_native_relay",
                            check_run_url="https://api.github.com"+P+"/check-runs/8002", status="completed" if completed else "queued",
                            conclusion="success" if completed else None)]
        self.checks[8001] = self.make_check(8001, 503, "PR Gate", "completed" if completed else "in_progress", verdict if completed else None, "opaque-native")
        self.checks[8002] = self.make_check(8002, 503, "ng_native_relay", "completed" if completed else "queued", "success" if completed else None, "opaque-relay")
    def request(self, method, path, data=None):
        if method == "GET" and path == P+"/actions/runs/400/jobs?per_page=100":
            self.calls.append((method,path,copy.deepcopy(data)))
            return 200, dict(total_count=len(self.native_jobs), jobs=copy.deepcopy(self.native_jobs))
        for rid, r in self.controller_runs.items():
            if method == "GET" and path == P+"/actions/runs/"+str(rid):
                self.calls.append((method,path,copy.deepcopy(data))); return 200, copy.deepcopy(r)
        return super().request(method,path,data)

def published(i=0):
    api = FakeAPI(i)
    q = run("qualify",i)
    p = run("publish",i,api,q)
    return api,p

def baseline(i=0):
    api,p = published(i)
    b = run("baseline",i,api,p)
    return api,b

def native_result(i=0):
    api,b = baseline(i)
    api.start_native()
    n = run("gate",i,api,b)
    api.start_native(completed=True)
    return api,b,n

class NativeTests(unittest.TestCase):
    def test_inactive_job_without_url_cannot_whitelist_any_check_addition(self):
        api,b=baseline();api.start_native()
        api.native_jobs[1]["check_run_url"]=None
        del api.checks[8002]
        n=run("gate",api=api,doc=b)
        self.assertTrue(n["protocol_valid"],n)
        api.checks[8002]=api.make_check(8002,503,"ng_native_relay","queued",None,None)
        self.assertFalse(run("gate",api=api,doc=b)["protocol_valid"])
        api.native_jobs[0]["check_run_url"]=None
        self.assertFalse(run("gate",api=api,doc=b)["protocol_valid"])

    def test_successful_merge_may_no_longer_report_required_after_merge(self):
        api,b,n=native_result(3);api.post_merge_required=False
        p=run("probe",3,api,b,n)
        self.assertEqual(p["outcome"],"MERGED_NATIVE_PROBE_ONLY",p)
        self.assertTrue(p["advance"])

    def test_changed_source_job_binding_or_post_receipt_blocks_baseline(self):
        for change in ("job_attempt","job_head","receipt_status","receipt_missing"):
            api,p=published()
            if change=="job_attempt":api.jobs[0]["run_attempt"]=2
            if change=="job_head":api.jobs[0]["head_sha"]="e"*40
            if change=="receipt_status":p["evidence"]["receipts"][0]["status"]=200
            if change=="receipt_missing":p["evidence"]["receipts"]=[]
            api.calls.clear();r=run("baseline",api=api,doc=p)
            self.assertFalse(r["protocol_valid"],change)
            self.assertFalse(any(c[0]=="PUT" for c in api.calls),change)

    def test_native_inventory_conclusion_and_owning_suite_must_match_live_run(self):
        for change in ("suite","inventory"):
            api,b,n=native_result(3)
            if change=="suite":api.suites[503]["conclusion"]="failure"
            else:
                original=api.request
                def request(method,path,data=None):
                    status,body=original(method,path,data)
                    if "/actions/workflows/calibration.yml/runs?" in path:
                        for r in body["workflow_runs"]:
                            if r["id"]==400:r["conclusion"]="cancelled"
                    return status,body
                api.request=request
            api.calls.clear();r=run("probe",3,api,b,n)
            self.assertFalse(r["protocol_valid"],change)
            self.assertFalse(any(c[0]=="PUT" for c in api.calls),change)

    def test_skipped_native_relay_and_postmerge_bad_suite_cannot_complete_batch(self):
        api,b,n=native_result(3);api.native_jobs[1]["conclusion"]="skipped";api.calls.clear()
        r=run("probe",3,api,b,n)
        self.assertFalse(r["protocol_valid"])
        self.assertFalse(any(c[0]=="PUT" for c in api.calls))
        api,b,n=native_result(3);original=api.request
        def request(method,path,data=None):
            result=original(method,path,data)
            if method=="PUT":api.suites[503]["conclusion"]="failure"
            return result
        api.request=request
        r=run("probe",3,api,b,n)
        self.assertFalse(r["advance"])

    def test_transport_rejects_foreign_paths_payloads_and_duplicate_writes(self):
        m=manifest();env=dict(environment(m),GH_TOKEN="dummy")
        with patch.object(ng.API,"_send",return_value=(201,{})) as send:
            api=ng.API("publish",m,env)
            for method,path,payload in (("POST","/repos/other/repo/check-runs",{}),
                  ("PATCH",P+"/check-runs/8001",{}),("POST",P+"/issues/1/comments",{}),
                  ("PUT",P+"/branches/main/protection",{}),("GET",P+"/../other",None),
                  ("POST",P+"/check-runs",dict(ng.check_payload(m),name="foreign"))):
                with self.subTest(path=path),self.assertRaises(lab.LabError):api.request(method,path,payload)
            self.assertEqual(send.call_count,0)
            api.request("POST",P+"/check-runs",ng.check_payload(m))
            with self.assertRaises(lab.LabError):api.request("POST",P+"/check-runs",ng.check_payload(m))
            self.assertEqual(send.call_count,1)

    def test_transport_native_dispatch_digest_requires_validated_baseline_authorization(self):
        _,b=baseline();m=manifest();env=dict(environment(m),GH_TOKEN="dummy")
        payload=ng.dispatch_payload(ng.at_stage(m,"native-gate",baseline_run_id="300",baseline_digest="0"*64))
        with patch.object(ng.API,"_send",return_value=(204,{})) as send:
            api=ng.API("native-dispatch",m,env)
            with self.assertRaises(lab.LabError):api.request("POST",ng.DISPATCH,payload)
            api.authorize_baseline(b)
            with self.assertRaises(lab.LabError):api.request("POST",ng.DISPATCH,payload)
            valid=ng.dispatch_payload(ng.at_stage(m,"native-gate",baseline_run_id="300",baseline_digest=ng.digest(b)))
            api.request("POST",ng.DISPATCH,valid)
            self.assertEqual(send.call_count,1)
        m=manifest(stage="native-seed")
        with patch.object(ng.API,"_send",return_value=(204,{})) as send:
            api=ng.API("seed",m,dict(environment(m),GH_TOKEN="dummy"))
            p=ng.dispatch_payload(ng.at_stage(m,"native-qualification",seed_run_id="100"))
            bad=copy.deepcopy(p);bad["ref"]="main"
            with self.assertRaises(lab.LabError):api.request("POST",ng.DISPATCH,bad)
            api.request("POST",ng.DISPATCH,p)
            with self.assertRaises(lab.LabError):api.request("POST",ng.DISPATCH,p)
            self.assertEqual(send.call_count,1)

    def test_seed_and_qualification_relays_dispatch_once_with_real_source_ids(self):
        api=FakeAPI();api.runs.pop(200);api.runs[100].update(status="in_progress",conclusion=None)
        r=run("seed",api=api)
        self.assertTrue(r["protocol_valid"],r)
        self.assertEqual([c[1] for c in api.calls if c[0]=="POST"],[P+"/actions/workflows/calibration.yml/dispatches",P+"/actions/runs/100/cancel"])
        self.assertEqual(r["dispatched_manifest"]["seed_run_id"],"100")
        api=FakeAPI(2);q=run("qualify",2)
        r=run("qual-relay",2,api,q)
        self.assertTrue(r["protocol_valid"],r)
        self.assertEqual(r["dispatched_manifest"]["qualification_run_id"],"200")
        self.assertEqual(r["dispatched_manifest"]["stage"],"native-baseline")

    def test_native_relay_accepts_failed_gate_while_own_run_is_in_progress(self):
        api,b,n=native_result()
        api.runs[400].update(status="in_progress",conclusion=None)
        api.suites[503].update(status="in_progress",conclusion=None)
        api.native_jobs[1].update(status="in_progress",conclusion=None)
        api.checks[8002].update(status="in_progress",conclusion=None)
        r=run("native-relay",api=api,doc=b,native=n)
        self.assertTrue(r["protocol_valid"],r)
        self.assertEqual(r["dispatched_manifest"]["native_run_id"],"400")
        api.calls.clear();n["protocol_valid"]=False
        r=run("native-relay",api=api,doc=b,native=n)
        self.assertFalse(r["protocol_valid"])
        self.assertFalse(any(c[1].endswith("dispatches") for c in api.calls))

    def test_native_dispatch_requires_same_baseline_producer_and_current_requiredness(self):
        api,b=baseline();r=run("native-dispatch",api=api,doc=b)
        self.assertTrue(r["protocol_valid"],r)
        self.assertEqual(r["dispatched_manifest"]["baseline_run_id"],"300")
        self.assertEqual(r["dispatched_manifest"]["baseline_digest"],ng.digest(b))
        for change in ("producer","required","latest","base","duplicate_run"):
            api,b=baseline()
            if change=="producer":b["producer"]["run_id"]="301"
            if change=="required":api.required=False
            if change=="latest":api.latest_missing=True
            if change=="base":api.base_sha="e"*40
            if change=="duplicate_run":api.runs[201]=dict(api.runs[200],id=201)
            api.calls.clear();r=run("native-dispatch",api=api,doc=b)
            self.assertFalse(r["protocol_valid"],(change,r))
            self.assertFalse(any(c[1].endswith("dispatches") for c in api.calls))

    def test_advance_validates_full_proof_and_dispatches_exactly_next_or_finishes(self):
        for i in range(4):
            api,b,n=native_result(i);p=run("probe",i,api,b,n)
            m=manifest(i,"native-probe",b);api.calls.clear()
            a=ng.execute("advance",m,client=api,document=p,env=environment(m))
            self.assertTrue(a["protocol_valid"],a)
            if i==3:self.assertTrue(a["batch_complete"]);self.assertEqual(api.calls,[])
            else:
                self.assertEqual(a["dispatched_manifest"]["index"],i+1)
                self.assertEqual(a["dispatched_manifest"]["stage"],"native-seed")
                self.assertNotIn("seed_run_id",a["dispatched_manifest"])
            p["evidence"]["native"]["jobs"][1]["conclusion"]="failure";api.calls.clear()
            a=ng.execute("advance",m,client=api,document=p,env=environment(m))
            self.assertFalse(a["protocol_valid"]);self.assertEqual(api.calls,[])

    def test_negative_live_merged_readback_is_unsafe_even_after_http405(self):
        api,p=published()
        original=api.request
        def request(method,path,data=None):
            result=original(method,path,data)
            if method=="PUT":api.merged=True
            return result
        api.request=request
        r=run("baseline",api=api,doc=p)
        self.assertEqual(r["outcome"],"UNSAFE",r)

    def test_gate_cli_exit_and_output_distinguish_intended_failure_from_invalid_evidence(self):
        for i in range(4):
            api,b=baseline(i);api.start_native();m=manifest(i,"native-gate",b)
            with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,dict(environment(m),GITHUB_OUTPUT=d+"/outputs"),clear=True),patch.object(ng,"API",return_value=api):
                Path(d,"baseline.json").write_text(json.dumps(b))
                rc=ng.main(["gate","--input",d+"/baseline.json","--output",d+"/native.json"])
                n=json.loads(Path(d,"native.json").read_text())
                self.assertEqual(rc,0 if i==3 else 1)
                self.assertTrue(n["protocol_valid"],n)
                self.assertIn("gate_success="+str(i==3).lower(),Path(d,"outputs").read_text())

    def test_manifest_has_exact_four_independent_cases_and_stage_keys(self):
        self.assertEqual(ng.validate_manifest(manifest())["index"],0)
        mutations = [("index",4),("index",True),("stage","driver"),("controller_ref","main"),("experiment","other"),("seed_run_id","0")]
        for key,value in mutations:
            m=manifest();m[key]=value
            with self.subTest(key=key),self.assertRaises(lab.LabError): ng.validate_manifest(m)
        for key,value in (("head",CONTROLLER),("base", "a"*40),("head_ref","main"),("pr",True)):
            m=manifest();m["cases"][0][key]=value
            with self.subTest(key=key),self.assertRaises(lab.LabError): ng.validate_manifest(m)
        m=manifest();m["cases"].append(copy.deepcopy(CASES[0]))
        with self.assertRaises(lab.LabError): ng.validate_manifest(m)
    def test_real_process_qualification_keeps_pending_and_failure_separate_from_protocol(self):
        rows=[run("qualify",i) for i in range(4)]
        self.assertEqual([r["gate_status"] for r in rows],["awaiting-approval","awaiting-approval","failure","success"])
        self.assertEqual([len(r["deferred"]) for r in rows],[2,1,0,0])
        self.assertTrue(all(r["protocol_valid"] for r in rows))
        self.assertEqual(rows[2]["contracts"][0]["execution"]["returncode"],1)
        self.assertIn("synthetic failure",rows[2]["contracts"][0]["execution"]["stderr"])
    def test_baseline_and_native_end_to_end_four_cases_null_rollup_never_recovers(self):
        for i in range(4):
            with self.subTest(i=i):
                api,b,n=native_result(i)
                self.assertTrue(b["eligible"],b)
                self.assertTrue(n["protocol_valid"],n)
                self.assertEqual(n["gate_success"],i==3)
                p=run("probe",i,api,b,n)
                self.assertTrue(p["advance"],p)
                self.assertEqual(p["outcome"],"MERGED_NATIVE_PROBE_ONLY" if i==3 else "NEGATIVE_NATIVE_REFUSED")
                self.assertFalse(p["strict_recovery_verified"])
                self.assertEqual(p["evidence"]["native_check"]["id"],8001)
                self.assertNotEqual(api.native_jobs[0]["id"],8001)
                self.assertEqual(b["producer"]["run_id"],"300")
    def test_source_attempt_conclusion_or_head_change_prevents_native_and_final_put(self):
        for command in ("gate","probe"):
            for key,value in (("run_attempt",2),("head_sha","e"*40),("conclusion","cancelled")):
                api,b,n=native_result(3)
                if command=="gate": api.start_native()
                api.runs[200][key]=value;api.calls.clear()
                r=run(command,3,api,b,n)
                self.assertFalse(r["protocol_valid"],r)
                self.assertFalse(any(c[0]=="PUT" for c in api.calls))
    def test_skipped_gate_wrong_url_wrong_app_wrong_suite_reject_before_merge(self):
        for change in ("skipped","job_id","foreign_url","app","suite","unexpected_failure"):
            api,b,n=native_result(3)
            if change=="skipped": api.native_jobs[0]["conclusion"]="skipped"
            if change=="job_id": api.native_jobs[0]["check_run_url"]="https://api.github.com"+P+"/check-runs/90001"
            if change=="foreign_url": api.native_jobs[0]["check_run_url"]="https://evil.example/check-runs/8001"
            if change=="app": api.checks[8001]["app"]["id"]=1
            if change=="suite": api.checks[8001]["check_suite"]["id"]=501
            if change=="unexpected_failure": api.native_jobs[1]["conclusion"]="failure"
            api.calls.clear();r=run("probe",3,api,b,n)
            self.assertFalse(r["advance"],(change,r));self.assertFalse(any(c[0]=="PUT" for c in api.calls))
    def test_stale_green_forged_source_required_latest_and_foreign_change_fail_closed(self):
        for change in ("digest","producer","required","latest","foreign","extra"):
            api,b,n=native_result()
            if change=="digest": n["manifest"]["baseline_digest"]="0"*64
            if change=="producer": n["producer"]["run_id"]="200"
            if change=="required": api.required=False
            if change=="latest": api.latest_missing=True
            if change=="foreign": api.checks[11]["conclusion"]="success"
            if change=="extra": api.checks[8888]=api.make_check(8888,503,"foreign","completed","success",None)
            api.calls.clear();r=run("probe",0,api,b,n)
            self.assertFalse(r["advance"],(change,r));self.assertFalse(any(c[0]=="PUT" for c in api.calls))
    def test_other_context_denial_conflict_permissions_and_unsafe_stop_batch(self):
        for status,body,outcome in ((405,{"message":'Required status check "other" is expected.'},"INCONCLUSIVE"),
             (409,{"message":"conflict"},"INCONCLUSIVE"),(403,{"message":"Forbidden"},"INCONCLUSIVE"),
             (200,{"merged":True},"UNSAFE")):
            api,p=published();api.merge_result=status,body
            r=run("baseline",api=api,doc=p)
            self.assertEqual(r["outcome"],outcome,r);self.assertFalse(r["eligible"])
    def test_success_baseline_merge_validates_real_parents_and_ends_without_native(self):
        api,p=published(3);api.merge_result=200,{"merged":True}
        b=run("baseline",3,api,p)
        self.assertEqual(b["outcome"],"BASELINE_MERGED_NO_RECOVERY_NEEDED",b)
        self.assertFalse(b["eligible"]);self.assertTrue(b["advance"])
        a=run("advance",3,api,b)
        self.assertTrue(a["batch_complete"],a)
        api,p=published(3);api.merge_result=200,{"merged":True};api.merge_parents=["e"*40,CASES[3]["head"]]
        self.assertFalse(run("baseline",3,api,p)["advance"])
    def test_cli_always_writes_reports_and_outputs_and_negative_exit_nonzero(self):
        for i in range(4):
            m=manifest(i,"native-qualification")
            with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,dict(environment(m),GITHUB_OUTPUT=d+"/outputs"),clear=True):
                out=Path(d)/"qualification.json"
                self.assertEqual(ng.main(["qualify","--output",str(out)]),1 if i==2 else 0)
                self.assertTrue(json.loads(out.read_text())["protocol_valid"])
                self.assertIn("protocol_valid=true",Path(d,"outputs").read_text())
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{"NATIVE_MANIFEST":"broken"},clear=True):
            self.assertEqual(ng.main(["gate","--input",d+"/missing.json","--output",d+"/native.json"]),1)
            self.assertFalse(json.loads(Path(d,"native.json").read_text())["protocol_valid"])

if __name__ == "__main__": unittest.main()
