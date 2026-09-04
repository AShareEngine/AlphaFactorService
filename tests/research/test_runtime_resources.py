from argparse import Namespace
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from factor_service.research import remote_runner, runtime_resources
from factor_service.research.runtime_resources import (
    GIB, MIB, RuntimeResources, read_runtime_resources, snapshot_memory_estimate,
)


def _file(root, path, text):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(text), encoding="utf-8")


def _host(tmp_path):
    proc, cgroup = tmp_path / "proc", tmp_path / "cgroup"
    _file(proc, "meminfo", f"MemTotal: {512 * GIB // 1024} kB\nMemAvailable: {400 * GIB // 1024} kB")
    _file(proc, "self/cgroup", "0::/\n")
    return proc, cgroup


def test_cgroup_limit_and_reclaimable_cache_override_host_memory(tmp_path, monkeypatch):
    proc, cgroup = _host(tmp_path)
    _file(cgroup, "memory.max", 16 * GIB)
    _file(cgroup, "memory.current", 14 * GIB)
    _file(cgroup, "memory.stat", f"inactive_file {4 * GIB}")
    _file(cgroup, "memory.events", "oom 1\noom_kill 1")
    _file(proc, "42/status", "VmRSS: 5242880 kB")
    monkeypatch.setattr(runtime_resources.os, "cpu_count", lambda: 128)
    monkeypatch.setattr(runtime_resources.os, "sched_getaffinity", lambda _: set(range(8)), raising=False)

    result = read_runtime_resources(pid=42, proc_root=proc, cgroup_root=cgroup)

    assert result.memory_limit_bytes == 16 * GIB
    assert result.memory_available_bytes == 6 * GIB
    assert result.memory_source == "cgroup_v2"
    assert result.cpu_cores == 8
    assert result.process_rss_bytes == 5 * GIB
    assert result.oom_kill_count == 1
    assert result.reserve_bytes == int(2.4 * GIB)


def test_nested_unlimited_leaf_obeys_parent_and_cpu_quota(tmp_path, monkeypatch):
    proc, cgroup = _host(tmp_path)
    _file(proc, "self/cgroup", "0::/tenant/job\n")
    _file(cgroup, "tenant/job/memory.max", "max")
    _file(cgroup, "tenant/memory.max", 16 * GIB)
    _file(cgroup, "tenant/memory.current", 12 * GIB)
    _file(cgroup, "tenant/cpu.max", "600000 100000")
    monkeypatch.setattr(runtime_resources.os, "cpu_count", lambda: 128)
    monkeypatch.setattr(runtime_resources.os, "sched_getaffinity", lambda _: set(range(8)), raising=False)

    result = read_runtime_resources(proc_root=proc, cgroup_root=cgroup)

    assert result.memory_limit_bytes == 16 * GIB
    assert result.memory_available_bytes == 4 * GIB
    assert result.cpu_cores == 6


def test_v1_memory_and_cpu_limits(tmp_path, monkeypatch):
    proc, cgroup = _host(tmp_path)
    _file(proc, "self/cgroup", "5:memory:/job\n6:cpu,cpuacct:/job\n")
    _file(cgroup, "memory/job/memory.limit_in_bytes", 8 * GIB)
    _file(cgroup, "memory/job/memory.usage_in_bytes", 6 * GIB)
    _file(cgroup, "memory/job/memory.stat", f"total_inactive_file {GIB}")
    _file(cgroup, "cpu,cpuacct/job/cpu.cfs_quota_us", 350000)
    _file(cgroup, "cpu,cpuacct/job/cpu.cfs_period_us", 100000)
    monkeypatch.setattr(runtime_resources.os, "cpu_count", lambda: 128)
    monkeypatch.setattr(runtime_resources.os, "sched_getaffinity", lambda _: set(range(8)), raising=False)

    result = read_runtime_resources(proc_root=proc, cgroup_root=cgroup)

    assert result.memory_source == "cgroup_v1"
    assert result.memory_available_bytes == 3 * GIB
    assert result.cpu_cores == 3


def test_host_pressure_remains_binding_when_cgroup_has_free_memory(tmp_path):
    proc, cgroup = _host(tmp_path)
    _file(proc, "meminfo", f"MemTotal: {512 * GIB // 1024} kB\nMemAvailable: {GIB // 1024} kB")
    _file(cgroup, "memory.max", 16 * GIB)
    _file(cgroup, "memory.current", GIB)
    result = read_runtime_resources(proc_root=proc, cgroup_root=cgroup)
    assert result.memory_available_bytes == GIB
    assert result.training_headroom_bytes == 0


def test_unlimited_cgroup_falls_back_to_host_and_unknown_is_not_zero_capacity(tmp_path):
    proc, cgroup = _host(tmp_path)
    _file(cgroup, "memory.max", "max")
    result = read_runtime_resources(proc_root=proc, cgroup_root=cgroup)
    assert result.memory_limit_bytes == 512 * GIB
    assert result.memory_source == "host"
    missing = read_runtime_resources(proc_root=tmp_path / "missing", cgroup_root=tmp_path / "missing")
    assert missing.memory_source == "unavailable"


def test_snapshot_estimate_scales_with_data_not_window_count():
    estimate = snapshot_memory_estimate(6_340_347, 16)
    assert 4 * GIB < estimate < 5 * GIB
    assert snapshot_memory_estimate(12_680_694, 16) > estimate
    assert snapshot_memory_estimate(6_340_347, 100) > estimate


def _args(tmp_path):
    return Namespace(
        job_path=tmp_path / "job.json", work_dir=tmp_path / "work",
        result_path=tmp_path / "remote_result.json", artifact_root=tmp_path / "artifacts",
        progress_path=tmp_path / "progress.jsonl",
    )


def _resources(available=12 * GIB):
    return RuntimeResources(8, 16 * GIB, available, "cgroup_v2")


@pytest.mark.parametrize("resources,code", [
    (_resources(GIB), "node_memory_budget_exceeded"),
    (RuntimeResources(1, 0, 0, "unavailable"), "node_resource_unavailable"),
])
def test_supervisor_rejects_node_before_starting_child(tmp_path, monkeypatch, resources, code):
    monkeypatch.setattr(remote_runner, "read_runtime_resources", lambda **_: resources)
    monkeypatch.setattr(remote_runner.subprocess, "Popen", lambda *a, **k: pytest.fail("must not launch"))
    args = _args(tmp_path)
    assert remote_runner._supervise(args) == 78
    failure = json.loads((tmp_path / "remote_failure.json").read_text())
    assert failure["error_code"] == code
    assert not args.result_path.exists()


def test_supervisor_limits_threads_and_stops_only_owned_child_on_pressure(tmp_path, monkeypatch):
    args = _args(tmp_path)
    samples = iter([_resources(), _resources(), _resources(GIB)])
    monkeypatch.setattr(remote_runner, "read_runtime_resources", lambda **_: next(samples))
    monkeypatch.setattr(remote_runner.time, "sleep", lambda _: None)
    monkeypatch.setenv("ALPHA_EFFECTIVE_NUM_THREADS", "32")
    child = SimpleNamespace(pid=123, code=None)
    child.poll = lambda: child.code
    captured = {}

    def launch(command, **kwargs):
        captured.update(kwargs)
        assert "--training-child" in command
        remote_runner._append_progress(args.progress_path, {
            "stage": "walk_forward_training", "percent": 65,
            "details": {"window_index": 19, "window_count": 37},
        })
        return child

    killed = []

    def stop(process):
        if process.poll() is None:
            killed.append(process.pid)
            child.code = -15

    monkeypatch.setattr(remote_runner.subprocess, "Popen", launch)
    monkeypatch.setattr(remote_runner, "_stop_training_child", stop)
    assert remote_runner._supervise(args) == 78
    assert killed == [123]
    assert captured["start_new_session"] is True
    assert captured["env"]["ALPHA_EFFECTIVE_NUM_THREADS"] == "8"
    assert captured["env"]["OPENBLAS_NUM_THREADS"] == "8"
    events = [json.loads(line) for line in args.progress_path.read_text().splitlines()]
    assert events[-1]["details"]["window_index"] == 19
    assert events[-1]["details"]["resources"]["memory_limit_bytes"] == 16 * GIB
    assert events[-1]["stage"] == "node_memory_budget_exceeded"


def test_supervisor_reports_actual_oom_not_generic_exit_137(tmp_path, monkeypatch):
    samples = iter([_resources(), replace(_resources(), oom_kill_count=1)])
    monkeypatch.setattr(remote_runner, "read_runtime_resources", lambda **_: next(samples))
    child = SimpleNamespace(pid=123, poll=lambda: -9)
    monkeypatch.setattr(remote_runner.os, "killpg", lambda *args: None)
    monkeypatch.setattr(remote_runner.subprocess, "Popen", lambda *a, **k: child)
    assert remote_runner._supervise(_args(tmp_path)) == 137
    assert json.loads((tmp_path / "remote_failure.json").read_text())["error_code"] == "node_out_of_memory"


def test_supervisor_success_does_not_kill_or_create_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_runner, "read_runtime_resources", lambda **_: _resources())
    child = SimpleNamespace(pid=123, poll=lambda: 0)
    monkeypatch.setattr(remote_runner.os, "killpg", lambda *args: None)
    monkeypatch.setattr(remote_runner.subprocess, "Popen", lambda *a, **k: child)
    assert remote_runner._supervise(_args(tmp_path)) == 0
    assert not (tmp_path / "remote_failure.json").exists()


def test_progress_tail_waits_for_complete_append(tmp_path):
    path = tmp_path / "progress.jsonl"
    path.write_bytes(b'{"stage":"training","percent":64')
    tail = remote_runner._ProgressTail(path)
    assert tail.update()["stage"] == "remote_runtime_initialized"
    with path.open("ab") as target:
        target.write(b',"details":{"window_index":2}}\n')
    assert tail.update()["details"]["window_index"] == 2
    assert tail.update()["percent"] == 64


def test_memory_guard_terminates_only_its_real_training_process_group():
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    owned = subprocess.Popen(command, start_new_session=True)
    unrelated = subprocess.Popen(command, start_new_session=True)
    try:
        remote_runner._stop_training_child(owned)
        assert owned.poll() is not None
        assert unrelated.poll() is None
    finally:
        for process in (owned, unrelated):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_memory_guard_cleans_descendants_after_leader_exit(monkeypatch):
    calls = []
    monkeypatch.setattr(remote_runner.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    remote_runner._stop_training_child(SimpleNamespace(pid=123, poll=lambda: -9))
    assert calls == [(123, remote_runner.signal.SIGKILL)]


def test_dataset_memory_preflight_rejects_before_snapshot_loading(tmp_path, monkeypatch):
    from factor_service.research import job, trainer

    args = _args(tmp_path)
    args.job_path.write_text("{}", encoding="utf-8")
    dataset_hash = "a" * 64
    monkeypatch.setattr(job, "validate_job", lambda _: {"dataset_hash": dataset_hash})
    _file(args.artifact_root, f"datasets/{dataset_hash}/dataset_manifest.json", json.dumps({
        "row_count": 100_000_000, "feature_names": [f"f{i}" for i in range(16)],
    }))
    monkeypatch.setattr(remote_runner, "read_runtime_resources", lambda **_: _resources())
    monkeypatch.setattr(trainer.QlibTrainer, "train", lambda *a, **k: pytest.fail("must not load or train"))
    with pytest.raises(SystemExit) as caught:
        remote_runner._train(args)
    assert caught.value.code == 78
    failure = json.loads((tmp_path / "remote_failure.json").read_text())
    assert failure["error_code"] == "node_memory_budget_exceeded"
    assert "未加载完整数据集" in failure["message"]
