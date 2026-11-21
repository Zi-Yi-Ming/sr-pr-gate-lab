import argparse
import collections
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import time


def emit(transcript, kind, **fields):
    print(json.dumps({"event": kind, "unix": time.time(), **fields}, sort_keys=True), file=transcript, flush=True)


class Tee:
    def __init__(self, original, transcript):
        self.original = original
        self.transcript = transcript

    def write(self, text):
        self.transcript.write(text)
        self.transcript.flush()
        return self.original.write(text)

    def __getattr__(self, name):
        return getattr(self.original, name)


def runtime():
    directory = Path(os.environ["SR_DIAGNOSTIC_DIRECTORY"])
    with (directory / f"runtime-{os.getpid()}.log").open("a", buffering=1) as transcript:
        sys.stdout = Tee(sys.stdout, transcript)
        sys.stderr = Tee(sys.stderr, transcript)
        sys.argv = ["vllm_srun", *sys.argv[2:]]
        emit(transcript, "runtime_enter", pid=os.getpid(), subcommand=sys.argv[1] if len(sys.argv) > 1 else "")
        try:
            runpy.run_module("vllm_srun", run_name="__main__")
        except BaseException as err:
            emit(transcript, "runtime_exit", pid=os.getpid(), reason=type(err).__name__)
            raise
        finally:
            sys.stdout = sys.__stdout__
            sys.stderr = sys.__stderr__


def numbers(path, separator=None):
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {}
    return {
        cols[0].rstrip(":"): int(cols[1])
        for line in lines
        if len(cols := line.split(separator)) >= 2 and cols[1].isdigit()
    }


def read_field(path):
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    return int(value) if value.isdigit() else value


def cgroups():
    try:
        relative = next(
            line.removeprefix("0::").strip("/") for line in Path("/proc/self/cgroup").read_text().splitlines()
            if line.startswith("0::")
        )
    except (OSError, StopIteration):
        return []
    root = Path("/sys/fs/cgroup")
    current = root / relative
    return [path for path in (current, *current.parents) if path == root or root in path.parents]


def process_rss(root_pid):
    rows = subprocess.check_output(["ps", "-e", "-o", "pid=,ppid=,rss="], text=True)
    children = collections.defaultdict(list)
    rss = {}
    for line in rows.splitlines():
        pid, parent, kib = map(int, line.split())
        children[parent].append(pid)
        rss[pid] = kib
    group = [root_pid]
    for pid in group:
        group.extend(children[pid])
    return {"processes": sum(pid in rss for pid in group),
            "descendant_rss_sum_bytes": sum(rss.get(pid, 0) for pid in group) * 1024}


def oom_state(groups):
    return {
        "system": numbers(Path("/proc/vmstat")).get("oom_kill"),
        "cgroups": {str(group): {k: v for k, v in numbers(group / "memory.events").items()
                                 if k in {"oom", "oom_kill", "oom_group_kill"}}
                    for group in groups},
    }


def store(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def run(path, suite, directory):
    directory.mkdir(parents=True, exist_ok=True)
    begin = time.time()
    groups = cgroups()
    initial_oom = oom_state(groups)
    peaks = {}
    min_available = None
    latest = {}
    command = ["go", "test", "-v", "-run", "^$", "-bench", f"^Benchmark{suite}",
               "-benchmem", "-benchtime=3s", "-timeout=30m", "./benchmarks/..."]
    store(directory / "initial.json", {
        "start_unix": begin, "command": command, "cpus": len(os.sched_getaffinity(0)),
        "cgroup_limits": {str(g): read_field(g / "memory.max") for g in groups},
        "meminfo_kib": {k: v for k, v in numbers(Path("/proc/meminfo")).items()
                        if k in {"MemTotal", "MemAvailable", "SwapTotal"}},
        "oom": initial_oom,
    })
    child_env = dict(os.environ, SR_DIAGNOSTIC_DIRECTORY=str(directory),
                     VLLM_SRUN_COMMAND=f"{sys.executable} -u {Path(__file__).resolve()} runtime")
    with (directory / "benchmark.log").open("w") as bench_log, (directory / "samples.jsonl").open("w", buffering=1) as samples:
        child = subprocess.Popen(command, cwd=path / "perf", env=child_env,
                                 stdout=bench_log, stderr=subprocess.STDOUT)
        while True:
            available = numbers(Path("/proc/meminfo")).get("MemAvailable")
            if available is not None:
                min_available = available if min_available is None else min(min_available, available)
            usage = {str(g): read_field(g / "memory.current") for g in groups}
            for group, value in usage.items():
                if isinstance(value, int):
                    peaks[group] = max(peaks.get(group, 0), value)
            tree = process_rss(child.pid)
            peak_key = "benchmark_process_tree_rss"
            peaks[peak_key] = max(peaks.get(peak_key, 0), tree["descendant_rss_sum_bytes"])
            latest = {
                "elapsed_seconds": round(time.time() - begin, 1), "cgroup_current_bytes": usage,
                "peaks_bytes": peaks, "available_kib": available, "minimum_available_kib": min_available,
                "oom": oom_state(groups), **tree,
            }
            print(json.dumps(latest, sort_keys=True), file=samples, flush=True)
            store(directory / "latest.json", latest)
            try:
                status = child.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                if time.time() - begin > 42 * 60:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                    status = 124
                    break
    dmesg = subprocess.run(["sudo", "-n", "dmesg"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if dmesg.returncode == 0:
        events = [line for line in dmesg.stdout.splitlines()
                  if re.search(r"oom[-_ ]kill|out of memory|killed process", line, re.IGNORECASE)]
        (directory / "kernel-oom.log").write_text("\n".join(events) + "\n")
    store(directory / "result.json", {
        **latest, "status": status, "initial_oom": initial_oom, "final_oom": oom_state(groups),
        "kernel_oom_log_readable": dmesg.returncode == 0,
        "kernel_cgroup_peaks_bytes": {str(g): read_field(g / "memory.peak") for g in groups},
    })


def snapshot(directory, target, seconds):
    deadline = time.monotonic() + seconds + 30
    while not (directory / "result.json").exists() and time.monotonic() < deadline:
        initial = directory / "initial.json"
        if initial.exists() and time.time() - json.loads(initial.read_text())["start_unix"] >= seconds:
            break
        time.sleep(1)
    target.mkdir(parents=True, exist_ok=True)
    paths = sorted(directory.glob("*.log")) + sorted(directory.glob("*.json")) + sorted(directory.glob("*.jsonl"))
    for path in paths:
        shutil.copyfile(path, target / path.name)
    for filename in ("latest.json", "result.json"):
        file = target / filename
        if file.is_file():
            print(file.read_text(), end="")
    for file in sorted(target.glob("runtime-*.log")):
        print(f"--- {file.name} ---")
        print("\n".join(file.read_text(errors="replace").splitlines()[-8:]))


def supervisor_alive(directory):
    pid_file = directory / "supervisor.pid"
    if not pid_file.is_file():
        return False
    status = Path(f"/proc/{pid_file.read_text().strip()}/stat")
    try:
        return status.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def wait_for_result(directory):
    for _ in range(2700):
        status = directory / "result.json"
        if status.is_file():
            result = json.loads(status.read_text())
            print(json.dumps(result, indent=2))
            return result["status"]
        if not supervisor_alive(directory):
            detail = directory / "supervisor.log"
            if detail.is_file():
                print(detail.read_text(errors="replace"), file=sys.stderr)
            raise SystemExit("benchmark supervisor exited without recording a result")
        time.sleep(1)
    raise SystemExit("benchmark supervisor did not return a status")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "runtime":
        runtime()
    else:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="action", required=True)
        launch = sub.add_parser("run")
        launch.add_argument("path", type=Path)
        launch.add_argument("suite", choices=["Cache", "Classify"])
        launch.add_argument("directory", type=Path)
        check = sub.add_parser("snapshot")
        check.add_argument("directory", type=Path)
        check.add_argument("target", type=Path)
        check.add_argument("seconds", type=int)
        wait = sub.add_parser("wait")
        wait.add_argument("directory", type=Path)
        args = parser.parse_args()
        if args.action == "run":
            run(args.path, args.suite, args.directory)
        elif args.action == "snapshot":
            snapshot(args.directory, args.target, args.seconds)
        else:
            raise SystemExit(wait_for_result(args.directory))
