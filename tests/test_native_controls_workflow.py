"""Executable scheduling and fixed-source/permission contracts for controls."""
import json
import os
from pathlib import Path
import subprocess
import unittest
import sys
import tempfile
import yaml
from test_native_workflow import evaluate
import native_controls as c

ROOT=Path(__file__).resolve().parents[1]


class ControlWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.value=yaml.safe_load((ROOT/".github/workflows/calibration.yml").read_text());cls.jobs=cls.value["jobs"]

    def test_required_gate_really_runs_and_never_uses_required_name_in_other_stages(self):
        self.assertIn("cx_gate",self.jobs)
        job=self.jobs["cx_gate"]
        self.assertNotIn("needs",job)
        self.assertTrue(evaluate(job["if"],"control-gate"))
        self.assertEqual(evaluate(job["name"],"control-gate"),"PR Gate")
        for stage in ("control-seed","control-qualification","control-probe","native-gate","seed","qualification","driver"):
            self.assertFalse(evaluate(job["if"],stage))
            self.assertNotEqual(evaluate(job["name"],stage),"PR Gate")

    def test_old_workflow_definitions_unchanged_and_maximum_head_inventory_below_one_page(self):
        baseline=subprocess.run(["git","show","4bce44fdf2a2d422059f6eeb0c4799376c6a5f07:.github/workflows/calibration.yml"],cwd=ROOT,capture_output=True,text=True,check=True)
        old=yaml.safe_load(baseline.stdout)
        self.assertEqual(len(self.jobs),31)
        for key,value in old["jobs"].items():self.assertEqual(self.jobs[key],value,key)
        self.assertLess(3*len(self.jobs),100)
        self.assertEqual(self.value["concurrency"],old["concurrency"])

    def test_only_probe_merges_and_no_control_job_can_write_checks(self):
        jobs={k:j for k,j in self.jobs.items() if k.startswith("cx_")}
        self.assertEqual(len(jobs),8)
        for key,job in jobs.items():
            self.assertEqual(job["env"]["CONTROLS_MANIFEST"],"${{ inputs.manifest }}")
            self.assertNotEqual(job["permissions"].get("checks"),"write")
            if job["permissions"].get("contents")=="write":self.assertEqual(key,"cx_probe")
            if key in ("cx_seed","cx_qual_relay","cx_gate_relay","cx_advance"):self.assertEqual(job["permissions"].get("actions"),"write")
            checkout=next(x for x in job["steps"] if x.get("uses","").startswith("actions/checkout@"))
            self.assertEqual(checkout["with"],dict(ref="${{ github.sha }}",**{"persist-credentials":False}))

    def test_cross_run_downloads_are_pinned_and_relays_survive_failure_without_promoting_it(self):
        self.assertIn("cx_gate_relay",self.jobs)
        for key,stage in (("cx_qual_relay","control-qualification"),("cx_gate_relay","control-gate"),("cx_advance","control-probe")):
            self.assertTrue(evaluate(self.jobs[key]["if"],stage))
            self.assertFalse(evaluate(self.jobs[key]["if"],stage,cancelled=True))
        for key,field in (("cx_gate","qualification_run_id"),("cx_probe","gate_run_id")):
            download=next(x for x in self.jobs[key]["steps"] if x.get("uses","").startswith("actions/download-artifact@"))
            self.assertEqual(download["with"]["run-id"],"${{ fromJSON(inputs.manifest)."+field+" }}")
            self.assertEqual(download["with"]["github-token"],"${{ github.token }}")

    def test_failed_checkout_fallback_cannot_authorize_gate_or_merge(self):
        self.assertIn("cx_gate",self.jobs)
        for key,job in self.jobs.items():
            if not key.startswith("cx_") or key=="cx_execution":continue
            fallback=next(x for x in job["steps"] if x.get("id")=="fallback")
            script=fallback["run"].removeprefix("python3 - <<'PY'\n").removesuffix("\nPY\n").removesuffix("\nPY")
            with tempfile.TemporaryDirectory() as folder:
                output=Path(folder)/"outputs";env=dict(os.environ,GITHUB_OUTPUT=str(output))
                p=subprocess.run([sys.executable,"-c",script],cwd=folder,env=env,capture_output=True,text=True)
                self.assertEqual(p.returncode,0,(key,p.stderr))
                files=list((Path(folder)/"out").glob("*.json"));self.assertEqual(len(files),1)
                report=json.loads(files[0].read_text());self.assertFalse(report["protocol_valid"])
                self.assertFalse(report["gate_success"]);self.assertFalse(report["advance"])
                self.assertFalse(report["strict_recovery_verified"])
                self.assertNotIn("=true",output.read_text())


if __name__=="__main__":unittest.main()
