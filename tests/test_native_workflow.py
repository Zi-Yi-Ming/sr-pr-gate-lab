"""Validate the workflow's executable scheduling/permission/artifact boundary."""
from pathlib import Path
import re
import unittest
import json
import os
import subprocess
import sys
import tempfile
import yaml

PATH=Path(__file__).resolve().parents[1]/".github/workflows/calibration.yml"

def evaluate(expression,stage,**variables):
    text=str(expression).removeprefix("${{").removesuffix("}}").strip()
    text=text.replace("fromJSON(inputs.manifest).stage",repr(stage))
    text=text.replace("always()","True").replace("cancelled()",str(variables.pop("cancelled",False)))
    for key,value in variables.items():text=text.replace(key,repr(value))
    text=text.replace("&&"," and ").replace("||"," or ")
    text=re.sub(r"!(?!=)"," not ",text)
    return eval(text,{"__builtins__":{}},{})

class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.workflow=yaml.safe_load(PATH.read_text());cls.jobs=cls.workflow["jobs"]

    def test_native_gate_executes_even_when_dependencies_failed_and_inactive_elsewhere(self):
        self.assertIn("ng_gate",self.jobs,"missing native Gate job")
        job=self.jobs["ng_gate"]
        self.assertNotIn("needs",job)
        self.assertTrue(evaluate(job["if"],"native-gate"))
        self.assertEqual(evaluate(job["name"],"native-gate"),"PR Gate")
        for stage in ("native-seed","native-qualification","native-baseline","native-probe","seed","qualification","driver"):
            self.assertFalse(evaluate(job["if"],stage))
            self.assertNotEqual(evaluate(job["name"],stage),"PR Gate")

    def test_native_relays_survive_deliberate_failure_but_not_cancellation(self):
        self.assertIn("ng_native_relay",self.jobs)
        for name,stage in (("ng_qual_relay","native-qualification"),("ng_native_relay","native-gate")):
            job=self.jobs[name]
            self.assertTrue(evaluate(job["if"],stage))
            self.assertFalse(evaluate(job["if"],stage,cancelled=True))
            self.assertIn("always()",job["if"])
        self.assertEqual(self.jobs["ng_qualification"]["name"],"Native experiment qualification")
        self.assertEqual(self.jobs["ng_execution"]["name"],"Native experiment CI Execution")

    def test_all_native_jobs_have_isolated_permissions_and_frozen_checkout(self):
        native={k:v for k,v in self.jobs.items() if k.startswith("ng_")}
        self.assertEqual(len(native),11)
        for name,job in native.items():
            perms=job["permissions"]
            self.assertFalse(perms.get("checks")=="write" and perms.get("contents")=="write")
            if perms.get("checks")=="write":self.assertEqual(name,"ng_publish")
            if perms.get("contents")=="write":self.assertIn(name,("ng_baseline","ng_probe"))
            self.assertEqual(job["env"]["NATIVE_MANIFEST"],"${{ inputs.manifest }}")
            for step in job["steps"]:
                if "run" in step:self.assertNotIn("inputs.",step["run"])
                if "uses" in step:
                    self.assertRegex(step["uses"],r"@[0-9a-f]{40}$")
                    if "checkout@" in step["uses"]:
                        self.assertEqual(step["with"],{"ref":"${{ github.sha }}","persist-credentials":False})

    def test_artifact_downloads_pin_source_runs_and_uploads_survive_failed_commands(self):
        self.assertIn("ng_gate",self.jobs)
        expected={"ng_gate":{"native-baseline":"${{ fromJSON(inputs.manifest).baseline_run_id }}"},
                  "ng_probe":{"native-baseline":"${{ fromJSON(inputs.manifest).baseline_run_id }}",
                              "native-verdict":"${{ fromJSON(inputs.manifest).native_run_id }}"}}
        for name,artifacts in expected.items():
            downloads={s["with"]["name"]:s["with"] for s in self.jobs[name]["steps"] if "download-artifact@" in s.get("uses","")}
            for artifact,run in artifacts.items():self.assertEqual(downloads[artifact]["run-id"],run)
        for name,job in self.jobs.items():
            if not name.startswith("ng_") or name=="ng_execution":continue
            uploads=[s for s in job["steps"] if "upload-artifact@" in s.get("uses","")]
            self.assertEqual(len(uploads),1)
            self.assertEqual(uploads[0]["if"],"always()")
            self.assertEqual(uploads[0]["with"]["if-no-files-found"],"error")

    def test_checkout_failure_fallbacks_actually_create_parseable_fail_closed_reports(self):
        self.assertIn("ng_gate",self.jobs)
        for name,job in self.jobs.items():
            if not name.startswith("ng_") or name=="ng_execution":continue
            step=next(s for s in job["steps"] if s.get("name")=="Retain fail-closed evidence if checkout or command failed")
            code=step["run"].split("\n",1)[1].rsplit("\nPY",1)[0]
            output=next(s["with"]["path"] for s in job["steps"] if "upload-artifact@" in s.get("uses",""))
            with tempfile.TemporaryDirectory() as d:
                result=subprocess.run([sys.executable,"-I","-c",code],cwd=d,capture_output=True,text=True,
                                      env=dict(os.environ,GITHUB_OUTPUT=str(Path(d,"outputs"))))
                self.assertEqual(result.returncode,0,result.stderr)
                document=json.loads(Path(d,output).read_text())
                self.assertFalse(document["protocol_valid"])
                self.assertFalse(document["gate_success"])
                self.assertIn("protocol_valid=false",Path(d,"outputs").read_text())

if __name__=="__main__":unittest.main()
