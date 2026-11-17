"""Behavioral contracts for the two attribution controls."""
import copy
import json
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lab
import native_controls as c
import yaml

REPO = "vllm-project/semantic-router"  # forbidden destination, used only by negative guards
LAB = "Zi-Yi-Ming/sr-pr-gate-lab"
P = "/repos/"+LAB

BASE = "525bc8117c9176b158d206b324c904e18aab8534"
CONTROLLER = "a" * 40
CASES = [dict(case_id=f, fixture=f, pr=51+i, head=str(i+1)*40, base=BASE,
              base_ref="lab-target/control-"+f, head_ref="lab/control-"+f)
         for i, f in enumerate(("clean", "cancelled-no-gate"))]


def manifest(i=0, stage="control-qualification"):
    value = dict(schema_version=1, experiment="native-gate-controls-v1",
                 controller_ref="lab/native-controls", controller_sha=CONTROLLER,
                 cases=copy.deepcopy(CASES), index=i, stage=stage)
    if i and stage != "control-seed": value["seed_run_id"] = "100"
    if stage in ("control-gate", "control-probe"): value["qualification_run_id"] = "200"
    if stage == "control-probe": value["gate_run_id"] = "300"
    return value


class ManifestTests(unittest.TestCase):
    def test_clean_case_cannot_carry_cancelled_seed_evidence(self):
        value = manifest()
        value["seed_run_id"] = "100"
        with self.assertRaises(lab.LabError): c.validate_manifest(value)

    def test_cancelled_case_requires_seed_identity_after_seed(self):
        value = manifest(1)
        del value["seed_run_id"]
        with self.assertRaises(lab.LabError): c.validate_manifest(value)

    def test_clean_case_cannot_start_seed(self):
        with self.assertRaises(lab.LabError): c.validate_manifest(manifest(stage="control-seed"))

    def test_source_heads_and_pull_requests_must_be_independent(self):
        for key in ("head", "pr"):
            value = manifest();value["cases"][1][key] = value["cases"][0][key]
            with self.subTest(key=key), self.assertRaises(lab.LabError): c.validate_manifest(value)

    def test_old_targets_and_controller_are_not_accepted(self):
        for key, bad in (("base_ref", "main"), ("head_ref", "lab/native-success")):
            value = manifest();value["cases"][0][key] = bad
            with self.subTest(key=key), self.assertRaises(lab.LabError): c.validate_manifest(value)


def environment(m):
    controller = m["stage"] == "control-probe"
    ref = m["controller_ref"] if controller else CASES[m["index"]]["head_ref"]
    return dict(GITHUB_REPOSITORY=LAB, GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_RUN_ATTEMPT="1",
                GITHUB_RUN_ID={"control-seed":"100", "control-qualification":"200", "control-gate":"300", "control-probe":"400"}[m["stage"]],
                GITHUB_SHA=CONTROLLER if controller else CASES[m["index"]]["head"],
                GITHUB_REF="refs/heads/"+ref, GITHUB_WORKFLOW_REF=LAB+"/.github/workflows/calibration.yml@refs/heads/"+ref)


class Fixture:
    """Local HTTP boundary; API responses model actual run/check IDs separately."""
    def __init__(self, i=0, merge_status=405):
        self.i=i;self.case=copy.deepcopy(CASES[i]);self.calls=[];self.merge_status=merge_status;self.merged=False
        self.runs={};self.jobs={};self.suites={};self.checks={};self.required=True
        self.pr=dict(number=self.case["pr"],draft=False,state="open",merged=False,merged_at=None,
                     head=dict(sha=self.case["head"],ref=self.case["head_ref"],repo=dict(full_name=LAB)),
                     base=dict(sha=BASE,ref=self.case["base_ref"],repo=dict(full_name=LAB)))
        self.target=dict(ref="refs/heads/"+self.case["base_ref"],object=dict(type="commit",sha=BASE))
        original=yaml.safe_load((Path(__file__).resolve().parents[1]/".github/workflows/calibration.yml").read_text())["jobs"]
        names=[j.get("name",k) if k!="ng_gate" else "Native Gate inactive" for k,j in original.items() if not k.startswith("cx_")]
        names += ["cx_seed","Control qualification","Control CI Execution","cx_qual_relay","Control Gate inactive","cx_gate_relay","cx_probe","cx_advance"]
        self.names=names
        if i:self.add_run(100,"cancelled","seed")
        self.add_run(200,"success","qual")
        self.add_run(300,None,"gate")

    def add_run(self, rid, conclusion, role):
        suite=rid+500
        self.runs[rid]=dict(id=rid,path=".github/workflows/calibration.yml",event="workflow_dispatch",head_branch=self.case["head_ref"],
                            head_sha=self.case["head"],run_attempt=1,repository=dict(full_name=LAB),head_repository=dict(full_name=LAB),
                            status="completed" if conclusion else "in_progress",conclusion=conclusion,check_suite_id=suite)
        self.suites[suite]=dict(id=suite,head_sha=self.case["head"],app=dict(id=15368),status="completed" if conclusion else "in_progress",conclusion=conclusion)
        jobs=[]
        for pos,name in enumerate(self.names):
            if role=="gate" and name=="Control Gate inactive":name="PR Gate"
            result="skipped";status="completed"
            if role=="seed" and name=="cx_seed":result="cancelled"
            if role=="qual" and name in ("Control qualification","Control CI Execution","cx_qual_relay"):result="success"
            if role=="gate" and name=="PR Gate":status="completed" if conclusion else "in_progress";result=conclusion
            if role=="gate" and name=="cx_gate_relay":status="completed" if conclusion else "queued";result="success" if conclusion else None
            check_id=rid*100+pos+1
            jobs.append(dict(id=check_id+900000,run_id=rid,run_attempt=1,head_sha=self.case["head"],name=name,status=status,conclusion=result,
                             check_run_url="https://api.github.com"+P+"/check-runs/"+str(check_id)))
            self.checks[check_id]=dict(id=check_id,node_id="node"+str(check_id),name=name,status=status,conclusion=result,
                                      head_sha=self.case["head"],external_id=None,app=dict(id=15368),check_suite=dict(id=suite))
        self.jobs[rid]=jobs

    def finish(self):self.add_run(300,"success","gate")

    def request(self, method, path, data=None):
        self.calls.append((method,path,copy.deepcopy(data)))
        if method=="POST" and path=="/graphql":
            return 200,dict(data=dict(check=dict(id=data["variables"]["node"],isRequired=self.required),
                repository=dict(pullRequest=dict(headRefOid=self.case["head"]),object=dict(oid=self.case["head"],statusCheckRollup=None))))
        if method=="POST" and path.endswith("/dispatches"):return 204,None
        if method=="POST" and path.endswith("/cancel"):return 202,{}
        if method=="PUT":
            if self.merge_status==200:
                self.merged=True;self.required=False;self.pr.update(state="closed",merged=True,merged_at="2026-11-17T00:00:00Z");self.target["object"]["sha"]="f"*40
                return 200,dict(merged=True,sha="f"*40)
            return self.merge_status,dict(message='Required status check "PR Gate" is expected.' if self.merge_status==405 else 'Other check is failing')
        if path==P+"/pulls/"+str(self.case["pr"]):value=self.pr
        elif path==P+"/git/ref/heads/"+self.case["base_ref"]:value=self.target
        elif path.startswith(P+"/git/commits/"):value=dict(sha="f"*40,parents=[dict(sha=BASE),dict(sha=self.case["head"])])
        elif path.startswith(P+"/actions/workflows/"):value=dict(total_count=len(self.runs),workflow_runs=list(self.runs.values()))
        elif "/actions/runs/" in path:
            rid=int(path.split("/actions/runs/")[1].split("/")[0]);value=dict(total_count=len(self.jobs[rid]),jobs=self.jobs[rid]) if "/jobs?" in path else self.runs[rid]
        elif "/commits/" in path and "/check-runs?" in path:value=dict(total_count=len(self.checks),check_runs=list(self.checks.values()))
        elif "/commits/" in path and "/check-suites?" in path:value=dict(total_count=len(self.suites),check_suites=list(self.suites.values()))
        elif "/check-runs/" in path:value=self.checks[int(path.rsplit("/",1)[1])]
        elif "/check-suites/" in path:value=self.suites[int(path.rsplit("/",1)[1])]
        else:raise AssertionError((method,path,data))
        return 200,copy.deepcopy(value)


def qualified(i=0):
    m=manifest(i);return c.execute("qualify",m,env=environment(m))


def gated(i=0, merge_status=405):
    api=Fixture(i,merge_status);q=qualified(i);m=manifest(i,"control-gate")
    doc=c.execute("gate",m,client=api,document=q,env=environment(m));api.finish()
    return api,q,doc


def probe(i=0, api=None, doc=None):
    if api is None:api,q,doc=gated(i)
    m=manifest(i,"control-probe")
    return c.execute("probe",m,client=api,document=doc,env=environment(m))


class ControlFlowTests(unittest.TestCase):
    def test_clean_and_cancelled_no_gate_are_both_valid_positive_controls(self):
        for i in (0,1):
            api,q,doc=gated(i)
            self.assertTrue(q["protocol_valid"],q);self.assertTrue(doc["gate_success"],doc)
            p=probe(i,api,doc)
            self.assertEqual(p["outcome"],"CONTROL_STILL_BLOCKED",p)
            self.assertTrue(p["advance"],p)
            self.assertEqual(sum(x[0]=="PUT" for x in api.calls),1)

    def test_real_merge_requires_pr_parents_target_and_premerge_required(self):
        api,q,doc=gated(0,200);p=probe(0,api,doc)
        self.assertEqual(p["outcome"],"CONTROL_MERGED",p)
        self.assertTrue(p["advance"],p)

    def test_deferred_failed_or_old_qualification_never_allows_gate_success(self):
        for bad in ("deferred","failure","attempt","head","boolean_exit"):
            api=Fixture();q=qualified();m=manifest(stage="control-gate")
            if bad=="deferred":q["contracts"][1].update(status="deferred",execution=None)
            if bad=="failure":q["contracts"][0]["execution"]["returncode"]=1
            if bad=="attempt":q["producer"]["run_attempt"]="2"
            if bad=="head":q["producer"]["sha"]="e"*40
            if bad=="boolean_exit":q["contracts"][0]["execution"]["returncode"]=False
            doc=c.execute("gate",m,client=api,document=q,env=environment(m))
            self.assertFalse(doc["gate_success"],bad);self.assertFalse(doc["protocol_valid"],bad)
            self.assertFalse(any(x[0]=="PUT" for x in api.calls))

    def test_clean_cannot_silently_include_a_cancelled_suite(self):
        api=Fixture();api.add_run(100,"cancelled","seed");m=manifest(stage="control-gate")
        r=c.execute("gate",m,client=api,document=qualified(),env=environment(m))
        self.assertFalse(r["protocol_valid"],r)

    def test_cancelled_suite_must_not_contain_even_a_skipped_required_gate(self):
        for conclusion in ("success","skipped"):
            api=Fixture(1);job=api.jobs[100][0];job.update(name="PR Gate",conclusion=conclusion)
            check_id=int(job["check_run_url"].rsplit("/",1)[1]);api.checks[check_id].update(name="PR Gate",conclusion=conclusion)
            m=manifest(1,"control-gate");r=c.execute("gate",m,client=api,document=qualified(1),env=environment(m))
            self.assertFalse(r["protocol_valid"],r)

    def test_changed_source_required_gate_or_other_check_prevents_merge(self):
        for bad in ("head","base","attempt","required","skipped","foreign"):
            api,q,doc=gated()
            if bad=="head":api.pr["head"]["sha"]="e"*40
            if bad=="base":api.target["object"]["sha"]="e"*40
            if bad=="attempt":api.runs[200]["run_attempt"]=2
            if bad=="required":api.required=False
            if bad=="skipped":
                job=next(x for x in api.jobs[300] if x["name"]=="PR Gate");job["conclusion"]="skipped"
            if bad=="foreign":next(x for x in api.checks.values() if x["check_suite"]["id"]==700)["name"]="Foreign altered"
            api.calls.clear();r=probe(0,api,doc)
            self.assertFalse(r["protocol_valid"],bad);self.assertFalse(any(x[0]=="PUT" for x in api.calls),bad)

    def test_foreign_gate_id_cannot_be_substituted_for_real_job(self):
        api,q,doc=gated();job=next(x for x in api.jobs[300] if x["name"]=="PR Gate")
        job["check_run_url"]="https://api.github.com"+P+"/check-runs/9999"
        api.calls.clear();r=probe(0,api,doc)
        self.assertFalse(r["protocol_valid"],r);self.assertFalse(any(x[0]=="PUT" for x in api.calls))

    def test_other_denial_or_fake_merge_does_not_advance(self):
        api,q,doc=gated(0,409);r=probe(0,api,doc)
        self.assertFalse(r["protocol_valid"],r);self.assertFalse(r["advance"],r)

    def test_relays_bind_real_producers_and_advance_only_current_terminal_report(self):
        api,q,doc=gated();m=manifest()
        relay=c.execute("qual-relay",m,client=api,document=q,env=environment(m))
        self.assertEqual(relay["outcome"],"GATE_DISPATCHED",relay)
        m=manifest(stage="control-gate")
        relay=c.execute("gate-relay",m,client=api,document=doc,env=environment(m))
        self.assertEqual(relay["outcome"],"PROBE_DISPATCHED",relay)
        p=probe(0,api,doc);m=manifest(stage="control-probe")
        advance=c.execute("advance",m,client=api,document=p,env=environment(m))
        self.assertEqual(advance["outcome"],"NEXT_CONTROL_DISPATCHED",advance)
        self.assertEqual(advance["dispatched_manifest"],manifest(1,"control-seed"))
        p["producer"]["run_id"]="999";api.calls.clear()
        invalid=c.execute("advance",m,client=api,document=p,env=environment(m))
        self.assertFalse(invalid["protocol_valid"])
        self.assertFalse(any(x[0]=="POST" for x in api.calls))

    def test_last_control_ends_without_extra_dispatch(self):
        api,q,doc=gated(1);p=probe(1,api,doc);api.calls.clear();m=manifest(1,"control-probe")
        end=c.execute("advance",m,client=api,document=p,env=environment(m))
        self.assertTrue(end["batch_complete"],end);self.assertFalse(api.calls)

    def test_wrong_job_id_is_not_assumed_to_equal_check_run_id(self):
        api,q,doc=gated();job=next(x for x in api.jobs[300] if x["name"]=="PR Gate")
        self.assertNotEqual(job["id"],int(job["check_run_url"].rsplit("/",1)[1]))
        self.assertTrue(probe(0,api,doc)["protocol_valid"])

    def test_qualification_invalidated_after_first_probe_read_never_reaches_put(self):
        for invalid in ("pending","failure","new_attempt"):
            api,q,doc=gated();original=api.request;reads=[]
            def changing(method,path,data=None):
                if method=="GET" and path==P+"/actions/runs/200":
                    reads.append(path)
                    if len(reads)==2:
                        if invalid=="new_attempt":api.runs[200]["run_attempt"]=2
                        else:api.runs[200].update(status="in_progress" if invalid=="pending" else "completed",conclusion=None if invalid=="pending" else "failure")
                return original(method,path,data)
            api.request=changing;api.calls.clear();r=probe(0,api,doc)
            self.assertFalse(r["protocol_valid"],invalid)
            self.assertFalse(any(x[0]=="PUT" for x in api.calls),invalid)


class AllowlistTests(unittest.TestCase):
    def test_no_check_suite_rule_old_pr_or_production_mutations(self):
        m=manifest(stage="control-probe");env=dict(environment(m),GH_TOKEN="offline-test-only")
        with patch.object(c.API,"_send",return_value=(204,None)) as transport:
            api=c.API("probe",m,env)
            for method,path,data in (("POST",P+"/check-runs",{}),("PATCH",P+"/check-runs/1",{}),
                    ("DELETE",P+"/check-suites/1",None),("PUT",P+"/branches/main/protection",{}),
                    ("PUT",P+"/pulls/20/merge",dict(sha=CASES[0]["head"],merge_method="merge")),
                    ("PUT","/repos/"+REPO+"/pulls/1/merge",{}),
                    ("POST",P+"/actions/runs/1/rerun",None),("POST",P+"/issues/1/comments",{})):
                with self.subTest(method=method,path=path),self.assertRaises(lab.LabError):api.request(method,path,data)
            transport.assert_not_called()
            path=P+"/pulls/"+str(CASES[0]["pr"])+"/merge";payload=dict(sha=CASES[0]["head"],merge_method="merge")
            api.request("PUT",path,payload)
            with self.assertRaises(lab.LabError):api.request("PUT",path,payload)
            self.assertEqual(transport.call_count,1)

    def test_initial_dispatch_uses_clean_qualification_and_later_seed_requires_index_one(self):
        m=manifest();payload=c.dispatch_payload(m)
        self.assertEqual(payload["ref"],CASES[0]["head_ref"])
        self.assertEqual(json.loads(payload["inputs"]["manifest"])["stage"],"control-qualification")
        m=manifest(stage="control-gate");env=dict(environment(m),GH_TOKEN="offline-test-only")
        with patch.object(c.API,"_send",return_value=(204,None)) as transport:
            api=c.API("gate",m,env)
            with self.assertRaises(lab.LabError):api.request("POST",c.DISPATCH,c.dispatch_payload(m))
            transport.assert_not_called()
        api,q,doc=gated(0,200);original=api.request
        def wrong_parent(method,path,data=None):
            status,value=original(method,path,data)
            if "/git/commits/" in path:value["parents"][0]["sha"]="e"*40
            return status,value
        api.request=wrong_parent;r=probe(0,api,doc)
        self.assertFalse(r["protocol_valid"],r);self.assertFalse(r["advance"],r)


if __name__ == "__main__": unittest.main()
