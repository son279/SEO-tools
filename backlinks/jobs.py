"""Run backlink pipeline stages as background subprocesses.

Scripts live in this package; each stage runs with the data folder
(config.ROOT / BACKLINK_DATA_DIR) as its working directory, so the scripts'
relative paths (clients.txt, host_vertices/, backlink_index/...) resolve there.
"""
import subprocess
import sys
import time
import uuid

from . import config

_jobs = {}


def _exists(entry):
    if isinstance(entry, tuple):  # any one of these
        return any((config.ROOT / p).exists() for p in entry)
    return (config.ROOT / entry).exists()


def missing_inputs(stage):
    _, required = config.STAGES[stage]
    return [
        " or ".join(p) if isinstance(p, tuple) else p
        for p in required if not _exists(p)
    ]


def running_job():
    for job in _jobs.values():
        if job["proc"].poll() is None:
            return job
    return None


def start(stage, extra_args=None):
    if stage not in config.STAGES:
        raise ValueError(f"Unknown stage {stage!r}. Valid: {sorted(config.STAGES)}")
    busy = running_job()
    if busy:
        raise RuntimeError(f"Job {busy['id']} ({busy['stage']}) is still running.")
    missing = missing_inputs(stage)
    if missing:
        raise FileNotFoundError(
            f"Missing inputs for {stage} in {config.ROOT}: {missing}. "
            "Run the earlier stages first, or set BACKLINK_DATA_DIR to the folder holding your data."
        )
    script, _ = config.STAGES[stage]
    command = [script] if isinstance(script, str) else list(script)
    command += [str(a) for a in (extra_args or [])]
    script_path = config.PACKAGE_DIR / command[0]

    config.ROOT.mkdir(parents=True, exist_ok=True)
    config.JOB_LOG_DIR.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:8]
    log_path = config.JOB_LOG_DIR / f"{job_id}_{stage}.log"
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-u", str(script_path), *command[1:]],
        cwd=config.ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    _jobs[job_id] = {"id": job_id, "stage": stage, "proc": proc, "log": log_path,
                     "command": command, "started": time.time()}
    return job_id


def _get(job_id):
    if job_id not in _jobs:
        raise KeyError(f"Unknown job {job_id!r} (job ids are forgotten when the server restarts; "
                       f"logs stay in {config.JOB_LOG_DIR})")
    return _jobs[job_id]


def _state(code):
    return "running" if code is None else ("succeeded" if code == 0 else "failed")


def status(job_id, tail_lines=20):
    job = _get(job_id)
    code = job["proc"].poll()
    lines = job["log"].read_text(encoding="utf-8", errors="replace").splitlines()
    return {
        "job_id": job_id,
        "stage": job["stage"],
        "state": _state(code),
        "exit_code": code,
        "command": " ".join(job.get("command", [])),
        "elapsed_seconds": round(time.time() - job["started"]),
        "log_file": str(job["log"]),
        "log_tail": lines[-max(1, min(tail_lines, 200)):],
    }


def list_all():
    return [
        {"job_id": j["id"], "stage": j["stage"], "state": _state(j["proc"].poll()),
         "exit_code": j["proc"].poll(), "elapsed_seconds": round(time.time() - j["started"])}
        for j in sorted(_jobs.values(), key=lambda j: j["started"])
    ]


def cancel(job_id):
    job = _get(job_id)
    if job["proc"].poll() is None:
        job["proc"].terminate()
        try:
            job["proc"].wait(timeout=10)
        except subprocess.TimeoutExpired:
            job["proc"].kill()
    return status(job_id)
