from __future__ import annotations

from dataclasses import dataclass
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
from factor_service.research.dataset import DatasetBuilder
from factor_service.research.errors import RetryableJobError
from factor_service.research.job import CancellationToken
from factor_service.research.snapshot import DatasetSnapshotStore
from factor_service.research.trainer import TrainingResult
from factor_service.runtime_config import load_runtime_config, section


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")
_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]{0,63}$")
_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:-]{0,255}$")
_GPU_SPEC = re.compile(r"^[A-Za-z0-9=,._-]{0,128}$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")


@dataclass(frozen=True)
class RemoteNode:
    node_id: str
    name: str
    host: str
    port: int
    user: str
    work_dir: str
    docker_image: str
    gpus: str
    ssh_key: Path | None
    password_env: str
    known_hosts: Path | None
    enabled: bool
    cleanup_success: bool
    max_runtime_minutes: int

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
            "docker_image": self.docker_image,
            "gpus": self.gpus,
            "enabled": self.enabled,
            "available": self.enabled and self.credentials_available(),
            "credential_type": credential,
            "known_hosts_configured": self.known_hosts is not None,
            "max_runtime_minutes": self.max_runtime_minutes,
        }

    def credentials_available(self) -> bool:
        if self.ssh_key is not None:
            return self.ssh_key.is_file()
        return bool(self.password_env and os.environ.get(self.password_env))


def load_remote_nodes(runtime: dict[str, Any] | None = None) -> list[RemoteNode]:
    payload = runtime if runtime is not None else load_runtime_config()
    execution = section(payload, "research", "execution")
    raw_nodes = execution.get("remote_nodes") or []
    if not isinstance(raw_nodes, list):
        raise ValueError("research.execution.remote_nodes必须是数组")
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
        command = (
            "set -e; printf 'ssh=ok\\n'; command -v docker >/dev/null; "
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
        command = (
            "printf 'cpu_cores='; nproc 2>/dev/null || printf 0; "
            "printf '\\nload='; awk '{print $1}' /proc/loadavg 2>/dev/null || printf 0; "
            "printf '\\nmem='; free -m 2>/dev/null | awk '/^Mem:/{print $2\",\"$3}'; "
            "printf '\\ndisk='; df -Pk / 2>/dev/null | awk 'NR==2{print $2\",\"$3}'; "
            "printf '\\ngpu='; if command -v nvidia-smi >/dev/null 2>&1; then "
            "nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu "
            "--format=csv,noheader,nounits | tr '\\n' ';'; else printf unavailable; fi; "
            "printf '\\ncontainers='; docker ps --filter label=alphablocks.research=1 "
            "--format '{{.Names}}|{{.Status}}' 2>/dev/null | tr '\\n' ';'"
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
            if "|" in line:
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
            "gpus": gpus, "containers": containers,
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
            [*self._auth_prefix(), *self._ssh_base(), self._target(), command],
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
        args = [*self._auth_prefix(), "rsync", "-az", "--partial", "--protect-args"]
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
            *self._auth_prefix(), "rsync", "-az", "--partial", "--protect-args",
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
        container = f"ab-research-{job_id[-32:]}-{attempt:03d}"
        try:
            progress("remote_materializing_dataset", 4, {"node_id": self.node.node_id})
            builder = DatasetBuilder(self.settings)
            snapshot = self.snapshot_store.get_or_create(
                job, work_dir, builder,
                cancellation=cancellation, progress=progress,
            )
            cancellation.checkpoint()
            progress("remote_preparing", 57, {"node_id": self.node.node_id})
            mkdir = " ".join(shlex.quote(path) for path in (
                remote_root,
                f"{remote_root}/source/factor_service",
                f"{remote_root}/artifacts/datasets/{job['dataset_hash']}",
                f"{remote_root}/work",
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
            source_root = Path(__file__).resolve().parents[1]
            dataset_dir = snapshot.dataset_path.parent
            self.transport.push(
                source_root, f"{remote_root}/source/factor_service",
                directory=True, delete=True, cancellation=cancellation,
            )
            self.transport.push(
                dataset_dir, f"{remote_root}/artifacts/datasets/{job['dataset_hash']}",
                directory=True, delete=True, cancellation=cancellation,
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
            progress("remote_snapshot_uploaded", 60, {
                "node_id": self.node.node_id,
                "dataset_hash": job["dataset_hash"],
            })

            thread_count = max(1, int(
                ((job.get("config_json") or {}).get("model") or {}).get("params", {}).get("num_threads") or 4
            ))
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
                raise RetryableJobError(f"远程Docker启动失败: {launched.stderr.strip()[-1000:]}")
            progress("remote_training", 62, {
                "node_id": self.node.node_id, "container": container,
            })
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
        except BaseException:
            try:
                self.transport.ssh(
                    f"docker rm -f {shlex.quote(container)} >/dev/null 2>&1 || true",
                    timeout=30,
                )
            except Exception:
                pass
            raise
        finally:
            try:
                self.transport.ssh(
                    f"docker rm -f {shlex.quote(container)} >/dev/null 2>&1 || true",
                    timeout=30,
                )
            except Exception:
                pass

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
                    min(88, max(62, int(item.get("percent") or 0))),
                    {"node_id": self.node.node_id, **dict(item.get("details") or {})},
                )
            seen = len(lines)
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
                logs = self.transport.ssh(
                    f"docker logs --tail 200 {shlex.quote(container)} 2>&1 || true",
                    timeout=60,
                )
                raise RuntimeError(
                    f"远程训练容器异常结束(status={status}, exit={exit_code}):\n"
                    + logs.stdout[-20_000:]
                )
            time.sleep(2)


def _normalize_node(source: dict[str, Any]) -> RemoteNode:
    node_id = str(source.get("id") or "").strip()
    host = str(source.get("host") or "").strip()
    user = str(source.get("user") or "root").strip()
    work_dir = str(source.get("work_dir") or "/root/alphablocks-research").strip().rstrip("/")
    image = str(source.get("docker_image") or "alphafactor-research:latest").strip()
    gpus = str(source.get("gpus") or "all").strip()
    password_env = str(source.get("password_env") or "").strip()
    if not _IDENTIFIER.fullmatch(node_id) or node_id == "local":
        raise ValueError(f"远程训练节点ID无效: {node_id}")
    if not _HOST.fullmatch(host):
        raise ValueError(f"远程训练节点host无效: {host}")
    if not _USER.fullmatch(user):
        raise ValueError(f"远程训练节点user无效: {user}")
    if not work_dir.startswith("/") or any(part in {"", ".", ".."} for part in PurePosixPath(work_dir).parts[1:]):
        raise ValueError(f"远程训练work_dir必须是安全绝对路径: {work_dir}")
    if not _IMAGE.fullmatch(image):
        raise ValueError(f"远程训练docker_image无效: {image}")
    if not _GPU_SPEC.fullmatch(gpus):
        raise ValueError(f"远程训练gpus配置无效: {gpus}")
    if password_env and not _ENV_NAME.fullmatch(password_env):
        raise ValueError(f"远程训练password_env无效: {password_env}")
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
        docker_image=image,
        gpus=gpus,
        ssh_key=ssh_key,
        password_env=password_env,
        known_hosts=known_hosts,
        enabled=bool(source.get("enabled", True)),
        cleanup_success=bool(source.get("cleanup_success", True)),
        max_runtime_minutes=max(10, min(int(source.get("max_runtime_minutes") or 240), 1440)),
    )


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
    "execution_nodes", "get_remote_node", "load_remote_nodes",
]
