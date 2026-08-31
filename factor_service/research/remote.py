from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import time
from typing import Any, Callable

from factor_service.research.config import Settings
from factor_service.research.autodl import (
    AutoDLProClient,
    api_token_status,
    validate_instance_uuid,
    validate_token_environment,
)
from factor_service.research.dataset import DatasetBuilder
from factor_service.research.errors import RetryableJobError
from factor_service.research.job import CancellationToken
from factor_service.research.remote_node_repository import (
    get_remote_node_repository,
)
from factor_service.research.snapshot import DatasetSnapshotStore
from factor_service.research.trainer import TrainingResult
from factor_service.runtime_config import load_runtime_config, section


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")
_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]{0,63}$")
_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:-]{0,255}$")
_GPU_SPEC = re.compile(r"^[A-Za-z0-9=,._-]{0,128}$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_REMOTE_RUNNERS = {"docker", "direct_python"}
_LIFECYCLE_PROVIDERS = {"", "autodl_pro"}
_REMOTE_MAX_THREADS = 32
_REMOTE_VALIDATION_SAMPLE_ROWS = 200_000
_REMOTE_TRAIN_METRIC_SAMPLE_ROWS = 200_000


@dataclass(frozen=True)
class RemoteNode:
    node_id: str
    name: str
    host: str
    port: int
    user: str
    work_dir: str
    runner: str
    python_executable: str
    docker_image: str
    gpus: str
    ssh_key: Path | None
    password_env: str
    known_hosts: Path | None
    enabled: bool
    cleanup_success: bool
    max_runtime_minutes: int
    lifecycle_provider: str
    instance_uuid: str
    api_token_env: str
    auto_start: bool
    auto_stop: bool
    boot_timeout_minutes: int
    ssh_key_config: str = ""
    known_hosts_config: str = ""

    def public(self) -> dict[str, Any]:
        credential = (
            "ssh_key" if self.ssh_key is not None
            else "password_env" if self.password_env else "missing"
        )
        return {
            "id": self.node_id,
            "type": "remote",
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "work_dir": self.work_dir,
            "runner": self.runner,
            "python_executable": self.python_executable,
            "docker_image": self.docker_image,
            "gpus": self.gpus,
            "enabled": self.enabled,
            "available": (
                self.enabled
                and self.credentials_available()
                and (not self.lifecycle_provider or self.lifecycle_available())
            ),
            "credential_type": credential,
            "known_hosts_configured": self.known_hosts is not None,
            "max_runtime_minutes": self.max_runtime_minutes,
            "lifecycle_provider": self.lifecycle_provider or "manual",
            "instance_uuid": self.instance_uuid,
            "api_token_configured": bool(
                self.api_token_env
                and api_token_status(self.api_token_env)["configured"]
            ),
            "api_token_environment_configured": bool(
                self.api_token_env
                and api_token_status(self.api_token_env)["configured"]
            ),
            "auto_start": self.auto_start,
            "auto_stop": self.auto_stop,
            "boot_timeout_minutes": self.boot_timeout_minutes,
        }

    def credentials_available(self) -> bool:
        if self.ssh_key is not None:
            return self.ssh_key.is_file()
        return bool(self.password_env and os.environ.get(self.password_env))

    def lifecycle_available(self) -> bool:
        return bool(
            self.lifecycle_provider == "autodl_pro"
            and self.instance_uuid
            and self.api_token_env
            and api_token_status(self.api_token_env)["configured"]
        )


def load_remote_nodes(runtime: dict[str, Any] | None = None) -> list[RemoteNode]:
    if runtime is not None:
        raw_nodes = _runtime_remote_node_payloads(runtime)
    else:
        repository = get_remote_node_repository()
        if repository.legacy_import_required():
            runtime_payload = load_runtime_config()
            legacy_nodes = [
                _normalize_node(raw)
                for raw in _runtime_remote_node_payloads(runtime_payload)
            ]
            repository.import_legacy_once([
                remote_node_storage_payload(node) for node in legacy_nodes
            ])
        raw_nodes = repository.list_nodes()
    if not isinstance(raw_nodes, list):
        raise ValueError("PostgreSQL远程训练节点配置必须是数组")
    nodes: list[RemoteNode] = []
    seen: set[str] = set()
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            raise ValueError("远程训练节点配置必须是对象")
        node = _normalize_node(raw)
        if node.node_id in seen:
            raise ValueError(f"远程训练节点ID重复: {node.node_id}")
        seen.add(node.node_id)
        nodes.append(node)
    return nodes


def _runtime_remote_node_payloads(runtime: dict[str, Any]) -> list[dict[str, Any]]:
    execution = section(runtime, "research", "execution")
    raw_nodes = execution.get("remote_nodes") or []
    if not isinstance(raw_nodes, list):
        raise ValueError("research.execution.remote_nodes必须是数组")
    if any(not isinstance(raw, dict) for raw in raw_nodes):
        raise ValueError("远程训练节点配置必须是对象")
    return [dict(raw) for raw in raw_nodes]


def remote_node_storage_payload(node: RemoteNode) -> dict[str, Any]:
    return {
        "id": node.node_id,
        "name": node.name,
        "enabled": node.enabled,
        "host": node.host,
        "port": node.port,
        "user": node.user,
        "ssh_key": node.ssh_key_config or str(node.ssh_key or ""),
        "password_env": node.password_env,
        "known_hosts": node.known_hosts_config or str(node.known_hosts or ""),
        "work_dir": node.work_dir,
        "runner": node.runner,
        "python_executable": node.python_executable,
        "docker_image": node.docker_image,
        "gpus": node.gpus,
        "max_runtime_minutes": node.max_runtime_minutes,
        "cleanup_success": node.cleanup_success,
        "lifecycle_provider": node.lifecycle_provider,
        "instance_uuid": node.instance_uuid,
        "api_token_env": node.api_token_env,
        "auto_start": node.auto_start,
        "auto_stop": node.auto_stop,
        "boot_timeout_minutes": node.boot_timeout_minutes,
    }


def execution_nodes() -> list[dict[str, Any]]:
    local = {
        "id": "local",
        "type": "local",
        "name": "本机训练",
        "description": "AlphaFactorService短生命周期隔离进程",
        "enabled": True,
        "available": True,
    }
    return [local, *(node.public() for node in load_remote_nodes())]


def get_remote_node(node_id: str) -> RemoteNode:
    clean = str(node_id or "").strip()
    node = next((item for item in load_remote_nodes() if item.node_id == clean), None)
    if node is None:
        raise ValueError(f"远程训练节点未配置: {clean}")
    if not node.enabled:
        raise ValueError(f"远程训练节点已停用: {clean}")
    return node


class RemoteTransport:
    def __init__(self, node: RemoteNode) -> None:
        self.node = node
        self._ensure_client_tools()

    def test_connection(self) -> dict[str, Any]:
        if self.node.runner == "direct_python":
            python = shlex.quote(self.node.python_executable)
            dependency_check = shlex.quote(
                "import catboost, lightgbm, pandas, pyarrow, qlib, sklearn, torch, xgboost; "
                "print('python=ok'); print('torch=' + torch.__version__); "
                "print('cuda=' + str(torch.cuda.is_available()).lower())"
            )
            command = (
                "set -e; printf 'ssh=ok\\nrunner=direct_python\\n'; "
                f"test -x {python}; {python} -c {dependency_check}; "
                "if command -v nvidia-smi >/dev/null 2>&1; then "
                "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits; "
                "else printf 'gpu=unavailable\\n'; fi"
            )
        else:
            command = (
                "set -e; printf 'ssh=ok\\nrunner=docker\\n'; command -v docker >/dev/null; "
                "docker version --format 'docker={{.Server.Version}}'; "
                f"docker image inspect {shlex.quote(self.node.docker_image)} "
                "--format 'image=ok' >/dev/null; "
                "if command -v nvidia-smi >/dev/null 2>&1; then "
                "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits; "
                "else printf 'gpu=unavailable\\n'; fi"
            )
        completed = self.ssh(command, timeout=30)
        return {
            "success": completed.returncode == 0,
            "node": self.node.public(),
            "detail": completed.stdout.strip(),
            "error": completed.stderr.strip() if completed.returncode else "",
        }

    def collect_status(self) -> dict[str, Any]:
        runner_status = (
            "printf '\\ncontainers='; pgrep -af '[f]actor_service.research.remote_runner' "
            "2>/dev/null | tr '\\n' ';'"
            if self.node.runner == "direct_python"
            else "printf '\\ncontainers='; docker ps --filter label=alphablocks.research=1 "
            "--format '{{.Names}}|{{.Status}}' 2>/dev/null | tr '\\n' ';'"
        )
        command = (
            "printf 'cpu_cores='; nproc 2>/dev/null || printf 0; "
            "printf '\\nload='; awk '{print $1}' /proc/loadavg 2>/dev/null || printf 0; "
            "printf '\\nmem='; free -m 2>/dev/null | awk '/^Mem:/{print $2\",\"$3}'; "
            "printf '\\ndisk='; df -Pk / 2>/dev/null | awk 'NR==2{print $2\",\"$3}'; "
            "printf '\\ngpu='; if command -v nvidia-smi >/dev/null 2>&1; then "
            "nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu "
            "--format=csv,noheader,nounits | tr '\\n' ';'; else printf unavailable; fi; "
            + runner_status
        )
        try:
            completed = self.ssh(command, timeout=30)
        except Exception as exc:
            return {**self.node.public(), "online": False, "error": str(exc)}
        if completed.returncode != 0:
            return {
                **self.node.public(), "online": False,
                "error": completed.stderr.strip() or "SSH连接失败",
            }
        parsed = _parse_key_values(completed.stdout)
        gpus = []
        if parsed.get("gpu") and parsed["gpu"] != "unavailable":
            for line in parsed["gpu"].split(";"):
                parts = [item.strip() for item in line.split(",")]
                if len(parts) >= 5:
                    gpus.append({
                        "name": parts[0], "util": _int(parts[1]),
                        "mem_used_mb": _int(parts[2]), "mem_total_mb": _int(parts[3]),
                        "temp_c": _int(parts[4]),
                    })
        containers = []
        for line in str(parsed.get("containers") or "").split(";"):
            if self.node.runner == "direct_python" and line.strip():
                containers.append({"name": line.strip(), "status": "running"})
            elif "|" in line:
                name, status = line.split("|", 1)
                containers.append({"name": name, "status": status})
        mem = [_int(item) for item in str(parsed.get("mem") or "").split(",")]
        disk = [_int(item) for item in str(parsed.get("disk") or "").split(",")]
        return {
            **self.node.public(), "online": True,
            "cpu_cores": _int(parsed.get("cpu_cores")),
            "cpu_load": _float(parsed.get("load")),
            "mem_total_mb": mem[0] if mem else 0,
            "mem_used_mb": mem[1] if len(mem) > 1 else 0,
            "disk_total_kb": disk[0] if disk else 0,
            "disk_used_kb": disk[1] if len(disk) > 1 else 0,
            "gpus": gpus, "containers": containers, "runner": self.node.runner,
            "training_active": bool(containers),
        }

    def ssh(
        self,
        command: str,
        *,
        timeout: int,
        cancellation: CancellationToken | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self._run(
            [*self._auth_prefix(), *self._ssh_base(), command],
            timeout=timeout, cancellation=cancellation,
        )

    def push(
        self,
        source: Path,
        remote_path: str,
        *,
        directory: bool = False,
        delete: bool = False,
        cancellation: CancellationToken | None = None,
    ) -> None:
        # macOS ships openrsync 2.6.9, which does not support the newer
        # --protect-args flag. Node configuration is validated before it reaches
        # this transport and subprocess receives an argv list, so no shell
        # interpolation is involved here.
        # Parquet and model bundles are already compressed. Recompressing them
        # with rsync's -z consumes a CPU core and slows large AutoDL transfers.
        args = [*self._auth_prefix(), "rsync", "-a", "--partial"]
        if delete:
            args.append("--delete")
        if directory:
            args.extend(["--exclude", "__pycache__", "--exclude", "*.pyc"])
        args.extend(["-e", shlex.join(self._ssh_base(include_target=False))])
        local = str(source) + ("/" if directory else "")
        remote = f"{self._target()}:{remote_path}" + ("/" if directory else "")
        completed = self._run(args + [local, remote], timeout=3600, cancellation=cancellation)
        if completed.returncode != 0:
            raise RetryableJobError(f"推送远程文件失败: {completed.stderr.strip()[-1000:]}")

    def pull(
        self,
        remote_path: str,
        destination: Path,
        *,
        directory: bool = False,
        cancellation: CancellationToken | None = None,
    ) -> None:
        destination.mkdir(parents=True, exist_ok=True) if directory else destination.parent.mkdir(parents=True, exist_ok=True)
        args = [
            *self._auth_prefix(), "rsync", "-a", "--partial",
            "-e", shlex.join(self._ssh_base(include_target=False)),
        ]
        remote = f"{self._target()}:{remote_path}" + ("/" if directory else "")
        local = str(destination) + ("/" if directory else "")
        completed = self._run(args + [remote, local], timeout=3600, cancellation=cancellation)
        if completed.returncode != 0:
            raise RetryableJobError(f"拉取远程产物失败: {completed.stderr.strip()[-1000:]}")

    def _run(
        self,
        args: list[str],
        *,
        timeout: int,
        cancellation: CancellationToken | None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        if self.node.password_env:
            password = os.environ.get(self.node.password_env, "")
            if not password:
                raise ValueError(f"远程节点密码环境变量未设置: {self.node.password_env}")
            environment["SSHPASS"] = password
        process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=environment,
        )
        deadline = time.monotonic() + max(1, int(timeout))
        try:
            while process.poll() is None:
                if cancellation is not None:
                    cancellation.checkpoint()
                if time.monotonic() >= deadline:
                    process.terminate()
                    raise TimeoutError(f"远程命令超时: {args[-1][:160]}")
                time.sleep(0.2)
            stdout, stderr = process.communicate()
            return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
            raise

    def _auth_prefix(self) -> list[str]:
        return ["sshpass", "-e"] if self.node.password_env else []

    def _ssh_base(self, *, include_target: bool = True) -> list[str]:
        args = [
            "ssh", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3", "-p", str(self.node.port),
        ]
        if self.node.known_hosts is not None:
            args.extend([
                "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={self.node.known_hosts}",
            ])
        else:
            args.extend(["-o", "StrictHostKeyChecking=accept-new"])
        if self.node.ssh_key is not None:
            args.extend(["-o", "BatchMode=yes", "-i", str(self.node.ssh_key)])
        else:
            args.extend(["-o", "BatchMode=no"])
        if include_target:
            args.append(self._target())
        return args

    def _target(self) -> str:
        return f"{self.node.user}@{self.node.host}"

    def _ensure_client_tools(self) -> None:
        required = ["ssh", "rsync", *( ["sshpass"] if self.node.password_env else [])]
        missing = [name for name in required if shutil.which(name) is None]
        if missing:
            raise RuntimeError("本机缺少远程训练命令: " + ", ".join(missing))
        if not self.node.credentials_available():
            raise ValueError(f"远程节点{self.node.node_id}认证信息不可用")


class RemoteResearchExecutor:
    def __init__(self, settings: Settings, node: RemoteNode) -> None:
        self.settings = settings
        self.node = node
        self.transport = RemoteTransport(node)
        self.lifecycle = autodl_client(node) if node.lifecycle_provider else None
        self.snapshot_store = DatasetSnapshotStore(settings.model_artifacts_root)

    def train(
        self,
        job: dict[str, Any],
        work_dir: Path,
        *,
        cancellation: CancellationToken,
        progress: Callable[[str, int, dict[str, Any]], None],
    ) -> TrainingResult:
        job_id = str(job["job_id"])
        attempt = max(1, int(job.get("attempt_count") or 1))
        remote_root = f"{self.node.work_dir}/runs/{job_id}/attempt-{attempt:03d}"
        cache_root = f"{self.node.work_dir}/cache"
        container = f"ab-research-{job_id[-32:]}-{attempt:03d}"
        progress("remote_materializing_dataset", 4, {"node_id": self.node.node_id})
        builder = DatasetBuilder(self.settings)
        snapshot = self.snapshot_store.get_or_create(
            job, work_dir, builder,
            cancellation=cancellation, progress=progress,
        )
        progress("remote_dataset_staged", 56, {
            "node_id": self.node.node_id,
            "dataset_hash": job["dataset_hash"],
            "snapshot_reused": snapshot.reused,
        })
        cancellation.checkpoint()
        remote_ready = False
        lifecycle_attempted = False
        try:
            lifecycle_attempted = self.lifecycle is not None
            self._prepare_lifecycle(cancellation=cancellation, progress=progress)
            remote_ready = True
            progress("remote_preparing", 60, {"node_id": self.node.node_id})
            source_root = Path(__file__).resolve().parents[1]
            source_fingerprint = _source_fingerprint(source_root)
            source_cache = f"{cache_root}/source/{source_fingerprint}"
            dataset_cache = f"{cache_root}/datasets/{job['dataset_hash']}"
            mkdir = " ".join(shlex.quote(path) for path in (
                remote_root,
                f"{remote_root}/source",
                f"{remote_root}/artifacts/datasets",
                f"{remote_root}/work",
                f"{cache_root}/source",
                f"{cache_root}/datasets",
            ))
            completed = self.transport.ssh(
                f"mkdir -p {mkdir}", timeout=60, cancellation=cancellation,
            )
            if completed.returncode != 0:
                raise RetryableJobError(completed.stderr.strip() or "无法创建远程任务目录")

            descriptor = work_dir / "remote_job.json"
            descriptor.write_text(
                json.dumps(job, ensure_ascii=False, sort_keys=True, default=str),
                encoding="utf-8",
            )
            dataset_dir = snapshot.dataset_path.parent
            source_cache_hit = self._remote_cache_ready(source_cache, cancellation)
            if not source_cache_hit:
                self.transport.push(
                    source_root, source_cache,
                    directory=True, delete=True, cancellation=cancellation,
                )
                self._mark_remote_cache_ready(source_cache, cancellation)
            dataset_cache_hit = self._remote_cache_ready(dataset_cache, cancellation)
            if not dataset_cache_hit:
                self.transport.push(
                    dataset_dir, dataset_cache,
                    directory=True, delete=True, cancellation=cancellation,
                )
                self._mark_remote_cache_ready(dataset_cache, cancellation)
            links = (
                f"rm -rf -- {shlex.quote(remote_root + '/source/factor_service')} "
                f"{shlex.quote(remote_root + '/artifacts/datasets/' + str(job['dataset_hash']))}; "
                f"ln -s {shlex.quote(source_cache)} "
                f"{shlex.quote(remote_root + '/source/factor_service')}; "
                f"ln -s {shlex.quote(dataset_cache)} "
                f"{shlex.quote(remote_root + '/artifacts/datasets/' + str(job['dataset_hash']))}"
            )
            linked = self.transport.ssh(
                links, timeout=60, cancellation=cancellation,
            )
            if linked.returncode != 0:
                raise RetryableJobError(
                    linked.stderr.strip() or "无法挂载远程训练缓存"
                )
            incremental = dict((job.get("config_json") or {}).get("incremental_training") or {})
            source_artifact = dict(incremental.get("source_artifact") or {})
            source_relative = str(source_artifact.get("relative_path") or "").strip()
            if source_relative:
                source_path = self.snapshot_store.artifacts.resolve(source_relative)
                remote_source = f"{remote_root}/artifacts/{source_relative}"
                remote_parent = str(PurePosixPath(remote_source).parent)
                created = self.transport.ssh(
                    f"mkdir -p {shlex.quote(remote_parent)}",
                    timeout=60, cancellation=cancellation,
                )
                if created.returncode != 0:
                    raise RetryableJobError("无法创建远程增量模型目录")
                self.transport.push(
                    source_path, remote_source, cancellation=cancellation,
                )
            self.transport.push(
                descriptor, f"{remote_root}/job.json", cancellation=cancellation,
            )
            progress("remote_snapshot_uploaded", 61, {
                "node_id": self.node.node_id,
                "dataset_hash": job["dataset_hash"],
                "dataset_cache_hit": dataset_cache_hit,
                "source_cache_hit": source_cache_hit,
            })

            cpu_cores = self._remote_cpu_cores(cancellation)
            thread_count = _effective_remote_thread_count(
                cpu_cores, _requested_job_threads(job),
            )
            progress("remote_resources_ready", 62, {
                "node_id": self.node.node_id,
                "cpu_cores": cpu_cores,
                "training_threads": thread_count,
                "gpu_enabled": bool(self.node.gpus and self.node.gpus != "0"),
                    "amp_enabled": bool(self.node.gpus and self.node.gpus != "0"),
                    "validation_sample_rows": _REMOTE_VALIDATION_SAMPLE_ROWS,
                    "train_metric_sample_rows": _REMOTE_TRAIN_METRIC_SAMPLE_ROWS,
            })
            if self.node.runner == "direct_python":
                command = self._direct_python_command(remote_root, thread_count)
            else:
                docker = [
                    "docker", "run", "-d", "--name", container,
                    "--label", "alphablocks.research=1",
                    "--label", f"alphablocks.job_id={job_id}",
                ]
                if self.node.gpus and self.node.gpus != "0":
                    docker.extend(["--gpus", self.node.gpus])
                docker.extend([
                    "-e", "PYTHONPATH=/opt/alphafactor",
                    "-e", f"OMP_NUM_THREADS={thread_count}",
                    "-e", f"MKL_NUM_THREADS={thread_count}",
                    "-e", f"ALPHA_TORCH_DEVICE={'cuda' if self.node.gpus and self.node.gpus != '0' else 'cpu'}",
                    "-e", f"ALPHA_MODEL_ACCELERATOR={'cuda' if self.node.gpus and self.node.gpus != '0' else 'cpu'}",
                    "-e", f"ALPHA_EFFECTIVE_NUM_THREADS={thread_count}",
                    "-e", "ALPHA_TORCH_AMP=1",
                    "-e", "ALPHA_TORCH_TF32=1",
                    "-e", "ALPHA_TORCH_AUTO_BATCH=1",
                    "-e", f"ALPHA_VALIDATION_SAMPLE_ROWS={_REMOTE_VALIDATION_SAMPLE_ROWS}",
                    "-e", f"ALPHA_TRAIN_METRIC_SAMPLE_ROWS={_REMOTE_TRAIN_METRIC_SAMPLE_ROWS}",
                    "-v", f"{remote_root}:/workspace",
                    "-v", f"{remote_root}/source:/opt/alphafactor:ro",
                    self.node.docker_image,
                    "python", "-m", "factor_service.research.remote_runner",
                    "/workspace/job.json", "/workspace/work",
                    "/workspace/remote_result.json", "/workspace/artifacts",
                    "/workspace/progress.jsonl",
                ])
                command = "docker rm -f {name} >/dev/null 2>&1 || true; {run}".format(
                    name=shlex.quote(container), run=shlex.join(docker),
                )
            launched = self.transport.ssh(
                command, timeout=120, cancellation=cancellation,
            )
            if launched.returncode != 0:
                raise RetryableJobError(
                    f"远程{self.node.runner}启动失败: {launched.stderr.strip()[-1000:]}"
                )
            progress("remote_training", 63, {
                "node_id": self.node.node_id,
                "runner": self.node.runner,
                **({"container": container} if self.node.runner == "docker" else {}),
            })
            if self.node.runner == "direct_python":
                self._wait_for_process(
                    remote_root, cancellation=cancellation, progress=progress,
                )
            else:
                self._wait_for_container(
                    container, remote_root, cancellation=cancellation, progress=progress,
                )
            progress("remote_downloading", 88, {"node_id": self.node.node_id})
            self.transport.pull(
                f"{remote_root}/work", work_dir,
                directory=True, cancellation=cancellation,
            )
            result_path = work_dir / "remote_result.json"
            self.transport.pull(
                f"{remote_root}/remote_result.json", result_path,
                cancellation=cancellation,
            )
            result = _load_remote_result(
                result_path, work_dir, self.settings.model_artifacts_root,
            )
            if self.node.cleanup_success:
                self.transport.ssh(
                    f"rm -rf -- {shlex.quote(remote_root)}",
                    timeout=120, cancellation=cancellation,
                )
            return result
        finally:
            if remote_ready:
                self._stop_remote_runner(container, remote_root)
            if lifecycle_attempted:
                self._power_off_after_job(job, progress)

    def _remote_cache_ready(
        self, remote_path: str, cancellation: CancellationToken,
    ) -> bool:
        marker = shlex.quote(f"{remote_path}/.alphablocks-cache-complete")
        completed = self.transport.ssh(
            f"test -f {marker}", timeout=30, cancellation=cancellation,
        )
        return completed.returncode == 0

    def _mark_remote_cache_ready(
        self, remote_path: str, cancellation: CancellationToken,
    ) -> None:
        marker = shlex.quote(f"{remote_path}/.alphablocks-cache-complete")
        completed = self.transport.ssh(
            f"touch {marker}", timeout=30, cancellation=cancellation,
        )
        if completed.returncode != 0:
            raise RetryableJobError(
                completed.stderr.strip() or "无法写入远程缓存完成标记"
            )

    def _remote_cpu_cores(self, cancellation: CancellationToken) -> int:
        completed = self.transport.ssh(
            "getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || printf '4\\n'",
            timeout=30, cancellation=cancellation,
        )
        if completed.returncode != 0:
            return 4
        match = re.search(r"\d+", completed.stdout)
        return max(1, int(match.group(0))) if match else 4

    def _prepare_lifecycle(
        self,
        *,
        cancellation: CancellationToken,
        progress: Callable[[str, int, dict[str, Any]], None],
    ) -> None:
        if self.lifecycle is None:
            return
        progress("remote_checking_power", 57, {
            "node_id": self.node.node_id,
            "provider": self.node.lifecycle_provider,
        })
        cancellation.checkpoint()
        state = self.lifecycle.status()
        if state != "running":
            if not self.node.auto_start:
                raise RetryableJobError(
                    f"AutoDL实例未开机（当前状态: {state}），请先开机或启用自动开机",
                )
            progress("remote_powering_on", 58, {
                "node_id": self.node.node_id, "power_state": state,
            })
            self.lifecycle.power_on()
            state = self.lifecycle.wait_for_running(
                timeout_seconds=self.node.boot_timeout_minutes * 60,
                checkpoint=cancellation.checkpoint,
                on_status=lambda current: progress("remote_powering_on", 58, {
                    "node_id": self.node.node_id, "power_state": current,
                }),
            )
        snapshot = self.lifecycle.snapshot()
        self.node = node_with_autodl_endpoint(self.node, snapshot)
        self.transport = RemoteTransport(self.node)
        progress("remote_waiting_for_ssh", 59, {
            "node_id": self.node.node_id,
            "power_state": state,
            "host": self.node.host,
            "port": self.node.port,
        })
        deadline = time.monotonic() + self.node.boot_timeout_minutes * 60
        last_error = ""
        while time.monotonic() < deadline:
            cancellation.checkpoint()
            try:
                completed = self.transport.ssh(
                    "true", timeout=20, cancellation=cancellation,
                )
                if completed.returncode == 0:
                    return
                last_error = completed.stderr.strip()[-500:]
            except Exception as exc:
                last_error = str(exc)[-500:]
            time.sleep(3)
        raise TimeoutError(
            f"AutoDL实例已开机但SSH未就绪: {last_error or '连接超时'}",
        )

    def _power_off_after_job(
        self,
        job: dict[str, Any],
        progress: Callable[[str, int, dict[str, Any]], None],
    ) -> None:
        if self.lifecycle is None or not self.node.auto_stop:
            return
        if self._experiment_has_pending_remote_jobs(job):
            try:
                progress("remote_kept_alive", 89, {
                    "node_id": self.node.node_id,
                    "reason": "experiment_jobs_pending",
                })
            except Exception:
                pass
            return
        try:
            progress("remote_powering_off", 89, {
                "node_id": self.node.node_id,
            })
        except Exception:
            pass
        try:
            self.lifecycle.power_off()
        except Exception as exc:
            # A shutdown failure must be visible without retrying an already
            # completed and potentially expensive training run.
            try:
                progress("remote_power_off_failed", 89, {
                    "node_id": self.node.node_id,
                    "error": str(exc)[:500],
                })
            except Exception:
                pass

    def _experiment_has_pending_remote_jobs(self, job: dict[str, Any]) -> bool:
        config = dict(job.get("config_json") or {})
        experiment_id = str(
            dict(config.get("experiment") or {}).get("experiment_id") or ""
        ).strip()
        if not experiment_id:
            return False
        try:
            from factor_service.model_research_repository import (
                ModelResearchRepository,
            )

            jobs = ModelResearchRepository().list_jobs(
                experiment_id=experiment_id,
                limit=100,
            )
        except Exception:
            return False
        current_job_id = str(job.get("job_id") or "")
        for candidate in jobs:
            if str(candidate.get("job_id") or "") == current_job_id:
                continue
            if str(candidate.get("status") or "") not in {
                "queued", "leased", "running", "uploading",
            }:
                continue
            execution = dict(
                dict(candidate.get("config_json") or {}).get("execution") or {}
            )
            dataset_hash = str(candidate.get("dataset_hash") or "").strip().lower()
            if (
                str(execution.get("node_id") or "local") == self.node.node_id
                and self._local_dataset_snapshot_ready(dataset_hash)
            ):
                return True
        return False

    def _local_dataset_snapshot_ready(self, dataset_hash: str) -> bool:
        if not dataset_hash:
            return False
        root = self.snapshot_store.artifacts.root / "datasets" / dataset_hash
        return all((root / name).is_file() for name in (
            "dataset.parquet", "dataset_raw.parquet", "dataset_manifest.json",
        ))

    def _direct_python_command(self, remote_root: str, thread_count: int) -> str:
        gpu_enabled = bool(self.node.gpus and self.node.gpus != "0")
        paths = {
            "job": f"{remote_root}/job.json",
            "work": f"{remote_root}/work",
            "result": f"{remote_root}/remote_result.json",
            "artifacts": f"{remote_root}/artifacts",
            "progress": f"{remote_root}/progress.jsonl",
            "log": f"{remote_root}/runner.log",
            "pid": f"{remote_root}/runner.pid",
            "exit": f"{remote_root}/runner.exit",
        }
        runner = [
            "env",
            f"PYTHONPATH={remote_root}/source",
            f"OMP_NUM_THREADS={thread_count}",
            f"MKL_NUM_THREADS={thread_count}",
            f"ALPHA_TORCH_DEVICE={'cuda' if gpu_enabled else 'cpu'}",
            f"ALPHA_MODEL_ACCELERATOR={'cuda' if gpu_enabled else 'cpu'}",
            f"ALPHA_EFFECTIVE_NUM_THREADS={thread_count}",
            "ALPHA_TORCH_AMP=1",
            "ALPHA_TORCH_TF32=1",
            "ALPHA_TORCH_AUTO_BATCH=1",
            f"ALPHA_VALIDATION_SAMPLE_ROWS={_REMOTE_VALIDATION_SAMPLE_ROWS}",
            f"ALPHA_TRAIN_METRIC_SAMPLE_ROWS={_REMOTE_TRAIN_METRIC_SAMPLE_ROWS}",
            self.node.python_executable,
            "-m", "factor_service.research.remote_runner",
            paths["job"], paths["work"], paths["result"], paths["artifacts"],
            paths["progress"],
        ]
        inner = (
            f"{shlex.join(runner)}; code=$?; "
            f"printf '%s\\n' \"$code\" > {shlex.quote(paths['exit'])}; exit \"$code\""
        )
        return (
            f"rm -f {shlex.quote(paths['exit'])} {shlex.quote(paths['pid'])}; "
            f"nohup setsid sh -c {shlex.quote(inner)} > {shlex.quote(paths['log'])} 2>&1 < /dev/null & "
            f"printf '%s\\n' \"$!\" > {shlex.quote(paths['pid'])}"
        )

    def _stop_remote_runner(self, container: str, remote_root: str) -> None:
        try:
            if self.node.runner == "direct_python":
                pid_path = shlex.quote(f"{remote_root}/runner.pid")
                command = (
                    f"if test -f {pid_path}; then pid=$(cat {pid_path}); "
                    "kill -- \"-$pid\" >/dev/null 2>&1 || "
                    "kill \"$pid\" >/dev/null 2>&1 || true; fi"
                )
            else:
                command = (
                    f"docker rm -f {shlex.quote(container)} >/dev/null 2>&1 || true"
                )
            self.transport.ssh(command, timeout=30)
        except Exception:
            pass

    def _wait_for_process(
        self,
        remote_root: str,
        *,
        cancellation: CancellationToken,
        progress: Callable[[str, int, dict[str, Any]], None],
    ) -> None:
        deadline = time.monotonic() + self.node.max_runtime_minutes * 60
        seen = 0
        exit_path = shlex.quote(f"{remote_root}/runner.exit")
        pid_path = shlex.quote(f"{remote_root}/runner.pid")
        while True:
            cancellation.checkpoint()
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"远程训练超过{self.node.max_runtime_minutes}分钟上限",
                )
            seen = self._forward_progress(
                remote_root, seen, cancellation=cancellation, progress=progress,
            )
            state = self.transport.ssh(
                f"if test -f {exit_path}; then printf 'exited '; cat {exit_path}; "
                f"elif test -f {pid_path} && kill -0 $(cat {pid_path}) 2>/dev/null; "
                "then printf 'running -1'; else printf 'missing -1'; fi",
                timeout=30, cancellation=cancellation,
            )
            if state.returncode != 0:
                raise RetryableJobError(
                    "远程训练状态检查失败: " + state.stderr.strip()[-1000:],
                )
            values = state.stdout.strip().split()
            process_state = values[0] if values else "missing"
            exit_code = values[1] if len(values) > 1 else "-1"
            if process_state == "exited" and exit_code == "0":
                return
            if (
                process_state == "exited"
                and _remote_result_is_complete(
                    self.transport, remote_root, cancellation=cancellation,
                )
            ):
                # Qlib/MLflow may fail during interpreter teardown while trying
                # to inspect a non-git remote work directory. The result file is
                # written atomically before the packaged marker, so both are a
                # stronger completion signal than that late cleanup exit code.
                progress("remote_process_cleanup_warning", 88, {
                    "node_id": self.node.node_id,
                    "exit_code": exit_code,
                    "result_complete": True,
                })
                return
            if process_state in {"exited", "missing"}:
                logs = self.transport.ssh(
                    f"tail -n 200 {shlex.quote(remote_root + '/runner.log')} 2>&1 || true",
                    timeout=60,
                )
                raise RuntimeError(
                    f"远程训练进程异常结束(status={process_state}, exit={exit_code}):\n"
                    + logs.stdout[-20_000:]
                )
            time.sleep(2)

    def _wait_for_container(
        self,
        container: str,
        remote_root: str,
        *,
        cancellation: CancellationToken,
        progress: Callable[[str, int, dict[str, Any]], None],
    ) -> None:
        deadline = time.monotonic() + self.node.max_runtime_minutes * 60
        seen = 0
        while True:
            cancellation.checkpoint()
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"远程训练超过{self.node.max_runtime_minutes}分钟上限",
                )
            seen = self._forward_progress(
                remote_root, seen, cancellation=cancellation, progress=progress,
            )
            inspect = self.transport.ssh(
                "docker inspect --format '{{.State.Status}} {{.State.ExitCode}}' "
                f"{shlex.quote(container)} 2>/dev/null || printf 'missing -1'",
                timeout=30, cancellation=cancellation,
            )
            if inspect.returncode != 0:
                raise RetryableJobError(
                    "远程训练状态检查失败: " + inspect.stderr.strip()[-1000:],
                )
            state = inspect.stdout.strip().split()
            status = state[0] if state else "missing"
            exit_code = state[1] if len(state) > 1 else "-1"
            if status in {"exited", "dead", "missing"}:
                if status == "exited" and exit_code == "0":
                    return
                if (
                    status == "exited"
                    and _remote_result_is_complete(
                        self.transport, remote_root, cancellation=cancellation,
                    )
                ):
                    progress("remote_process_cleanup_warning", 88, {
                        "node_id": self.node.node_id,
                        "exit_code": exit_code,
                        "result_complete": True,
                    })
                    return
                logs = self.transport.ssh(
                    f"docker logs --tail 200 {shlex.quote(container)} 2>&1 || true",
                    timeout=60,
                )
                raise RuntimeError(
                    f"远程训练容器异常结束(status={status}, exit={exit_code}):\n"
                    + logs.stdout[-20_000:]
                )
            time.sleep(2)

    def _forward_progress(
        self,
        remote_root: str,
        seen: int,
        *,
        cancellation: CancellationToken,
        progress: Callable[[str, int, dict[str, Any]], None],
    ) -> int:
        progress_result = self.transport.ssh(
            f"cat {shlex.quote(remote_root + '/progress.jsonl')} 2>/dev/null || true",
            timeout=30, cancellation=cancellation,
        )
        lines = [line for line in progress_result.stdout.splitlines() if line.strip()]
        for line in lines[seen:]:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            progress(
                f"remote.{item.get('stage') or 'training'}",
                min(88, max(63, int(item.get("percent") or 0))),
                {"node_id": self.node.node_id, **dict(item.get("details") or {})},
            )
        return len(lines)


def _remote_result_is_complete(
    transport: RemoteTransport,
    remote_root: str,
    *,
    cancellation: CancellationToken | None,
) -> bool:
    result_path = shlex.quote(f"{remote_root}/remote_result.json")
    progress_path = shlex.quote(f"{remote_root}/progress.jsonl")
    completed = transport.ssh(
        f"test -s {result_path} && grep -q '\"stage\": \"remote_packaged\"' "
        f"{progress_path}",
        timeout=30,
        cancellation=cancellation,
    )
    return completed.returncode == 0


def _source_fingerprint(source_root: Path) -> str:
    digest = sha256()
    files = sorted(
        path for path in source_root.rglob("*.py")
        if "__pycache__" not in path.parts
    )
    for path in files:
        digest.update(path.relative_to(source_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:24]


def _requested_job_threads(job: dict[str, Any]) -> int:
    model = dict((job.get("config_json") or {}).get("model") or {})
    candidates = [dict(model.get("params") or {}).get("num_threads")]
    candidates.extend(
        dict(item.get("params") or {}).get("num_threads")
        for item in list(model.get("base_models") or [])
        if isinstance(item, dict)
    )
    values = []
    for value in candidates:
        try:
            values.append(max(1, int(value)))
        except (TypeError, ValueError):
            continue
    return max(values, default=4)


def _effective_remote_thread_count(cpu_cores: int, requested: int) -> int:
    available = max(1, int(cpu_cores or 1))
    configured = max(1, int(requested or 1))
    automatic = max(4, available // 2)
    return min(available, _REMOTE_MAX_THREADS, max(configured, automatic))


def _normalize_node(source: dict[str, Any]) -> RemoteNode:
    node_id = str(source.get("id") or "").strip()
    host = str(source.get("host") or "").strip()
    user = str(source.get("user") or "root").strip()
    work_dir = str(source.get("work_dir") or "/root/alphablocks-research").strip().rstrip("/")
    runner = str(source.get("runner") or "docker").strip().lower()
    python_executable = str(source.get("python_executable") or "python").strip()
    image = str(source.get("docker_image") or "alphafactor-research:latest").strip()
    gpus = str(source.get("gpus") or "all").strip()
    password_env = str(source.get("password_env") or "").strip()
    lifecycle_provider = str(
        source.get("lifecycle_provider") or ""
    ).strip().lower()
    if lifecycle_provider in {"manual", "none"}:
        lifecycle_provider = ""
    instance_uuid = str(source.get("instance_uuid") or "").strip()
    api_token_env = str(source.get("api_token_env") or "").strip()
    if not _IDENTIFIER.fullmatch(node_id) or node_id == "local":
        raise ValueError(f"远程训练节点ID无效: {node_id}")
    if not _HOST.fullmatch(host):
        raise ValueError(f"远程训练节点host无效: {host}")
    if not _USER.fullmatch(user):
        raise ValueError(f"远程训练节点user无效: {user}")
    if not work_dir.startswith("/") or any(part in {"", ".", ".."} for part in PurePosixPath(work_dir).parts[1:]):
        raise ValueError(f"远程训练work_dir必须是安全绝对路径: {work_dir}")
    if runner not in _REMOTE_RUNNERS:
        raise ValueError("远程训练runner只允许docker或direct_python")
    if runner == "direct_python" and (
        not python_executable.startswith("/")
        or any(
            part in {"", ".", ".."}
            for part in PurePosixPath(python_executable).parts[1:]
        )
    ):
        raise ValueError("direct_python的python_executable必须是安全绝对路径")
    if not _IMAGE.fullmatch(image):
        raise ValueError(f"远程训练docker_image无效: {image}")
    if not _GPU_SPEC.fullmatch(gpus):
        raise ValueError(f"远程训练gpus配置无效: {gpus}")
    if password_env and not _ENV_NAME.fullmatch(password_env):
        raise ValueError(f"远程训练password_env无效: {password_env}")
    if lifecycle_provider not in _LIFECYCLE_PROVIDERS:
        raise ValueError("远程节点生命周期只支持manual或autodl_pro")
    if lifecycle_provider == "autodl_pro":
        instance_uuid = validate_instance_uuid(instance_uuid)
        api_token_env = validate_token_environment(api_token_env)
    else:
        instance_uuid = ""
        api_token_env = ""
    port = int(source.get("port") or 22)
    if not 1 <= port <= 65535:
        raise ValueError("远程训练SSH端口必须在1到65535之间")
    ssh_key_text = str(source.get("ssh_key") or "").strip()
    known_hosts_text = str(source.get("known_hosts") or "").strip()
    ssh_key = Path(ssh_key_text).expanduser().resolve() if ssh_key_text else None
    known_hosts = Path(known_hosts_text).expanduser().resolve() if known_hosts_text else None
    if not ssh_key and not password_env:
        raise ValueError(f"远程训练节点{node_id}必须配置ssh_key或password_env")
    return RemoteNode(
        node_id=node_id,
        name=str(source.get("name") or node_id).strip()[:80],
        host=host,
        port=port,
        user=user,
        work_dir=work_dir,
        runner=runner,
        python_executable=python_executable,
        docker_image=image,
        gpus=gpus,
        ssh_key=ssh_key,
        password_env=password_env,
        known_hosts=known_hosts,
        enabled=bool(source.get("enabled", True)),
        cleanup_success=bool(source.get("cleanup_success", True)),
        max_runtime_minutes=max(10, min(int(source.get("max_runtime_minutes") or 240), 1440)),
        lifecycle_provider=lifecycle_provider,
        instance_uuid=instance_uuid,
        api_token_env=api_token_env,
        auto_start=(
            bool(source.get("auto_start", False))
            if lifecycle_provider else False
        ),
        auto_stop=(
            bool(source.get("auto_stop", False))
            if lifecycle_provider else False
        ),
        boot_timeout_minutes=max(
            2, min(int(source.get("boot_timeout_minutes") or 15), 60),
        ),
        ssh_key_config=ssh_key_text,
        known_hosts_config=known_hosts_text,
    )


def autodl_client(node: RemoteNode) -> AutoDLProClient:
    if node.lifecycle_provider != "autodl_pro":
        raise ValueError(f"远程节点{node.node_id}未启用AutoDL Pro API")
    return AutoDLProClient(node.instance_uuid, node.api_token_env)


def node_with_autodl_endpoint(
    node: RemoteNode, snapshot: dict[str, Any],
) -> RemoteNode:
    host = str(snapshot.get("proxy_host") or "").strip()
    try:
        port = int(snapshot.get("ssh_port") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("AutoDL实例详情缺少有效SSH端口") from exc
    if not _HOST.fullmatch(host):
        raise ValueError("AutoDL实例详情缺少有效SSH地址")
    if not 1 <= port <= 65535:
        raise ValueError("AutoDL实例详情缺少有效SSH端口")
    return replace(node, host=host, port=port)


def _load_remote_result(
    path: Path, work_dir: Path, artifact_root: Path,
) -> TrainingResult:
    if not path.is_file():
        raise RuntimeError("远程训练没有返回结果描述文件")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "alphablocks.remote-training-result.v1":
        raise RuntimeError("远程训练结果协议版本不受支持")
    artifacts = []
    for item in payload.get("artifacts") or []:
        kind = str((item or {}).get("kind") or "")
        resolved = _resolve_scoped_path(item, work_dir, artifact_root)
        if not kind or not resolved.is_file():
            raise RuntimeError(f"远程训练产物不完整: {kind or 'unknown'}")
        artifacts.append((kind, resolved))
    predictions = _resolve_scoped_path(
        payload.get("predictions") or {}, work_dir, artifact_root,
    )
    if not predictions.is_file():
        raise RuntimeError("远程训练预测文件不存在")
    return TrainingResult(
        result=dict(payload.get("result") or {}),
        artifacts=artifacts,
        predictions_path=predictions,
    )


def _resolve_scoped_path(source: dict[str, Any], work_dir: Path, artifact_root: Path) -> Path:
    scope = str(source.get("scope") or "")
    root = work_dir.resolve() if scope == "work" else artifact_root.resolve() if scope == "artifact_root" else None
    if root is None:
        raise RuntimeError("远程训练产物scope无效")
    relative = PurePosixPath(str(source.get("path") or ""))
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise RuntimeError("远程训练产物路径无效")
    target = (root / Path(*relative.parts)).resolve()
    if root not in target.parents:
        raise RuntimeError("远程训练产物路径越界")
    return target


def _parse_key_values(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in str(value or "").splitlines():
        if "=" in line:
            key, item = line.split("=", 1)
            result[key.strip()] = item.strip()
    return result


def _int(value: Any) -> int:
    try:
        return int(float(str(value or 0).strip()))
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(str(value or 0).strip())
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "RemoteNode", "RemoteResearchExecutor", "RemoteTransport",
    "autodl_client", "execution_nodes", "get_remote_node", "load_remote_nodes",
    "node_with_autodl_endpoint", "remote_node_storage_payload",
]
