from __future__ import annotations

from copy import deepcopy
from contextlib import nullcontext
import os
import time
from typing import Any

import numpy as np
import pandas as pd


def _enabled_environment(name: str, *, default: bool = False) -> bool:
    raw = str(os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _configure_torch_runtime(torch: Any, device: Any) -> dict[str, Any]:
    profile = {
        "device": str(device),
        "amp_enabled": False,
        "tf32_enabled": False,
    }
    if device.type != "cuda":
        return profile
    amp_enabled = _enabled_environment("ALPHA_TORCH_AMP", default=True)
    tf32_enabled = _enabled_environment("ALPHA_TORCH_TF32", default=True)
    if tf32_enabled:
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
        if hasattr(torch, "set_float32_matmul_precision"):
            torch.set_float32_matmul_precision("high")
    profile.update({
        "amp_enabled": amp_enabled,
        "tf32_enabled": tf32_enabled,
        "gpu_name": torch.cuda.get_device_name(device),
        "gpu_memory_mb": int(
            torch.cuda.get_device_properties(device).total_memory / (1024 * 1024)
        ),
    })
    return profile


def _effective_torch_batch_size(torch: Any, device: Any, requested: int) -> int:
    batch_size = max(1, int(requested))
    if device.type != "cuda" or not _enabled_environment("ALPHA_TORCH_AUTO_BATCH"):
        return batch_size
    memory_gb = torch.cuda.get_device_properties(device).total_memory / (1024 ** 3)
    multiplier = 4 if memory_gb >= 24 else 2 if memory_gb >= 12 else 1
    return min(32_768, batch_size * multiplier)


def _autocast(torch: Any, device: Any, enabled: bool) -> Any:
    if device.type != "cuda" or not enabled:
        return nullcontext()
    if hasattr(torch, "autocast"):
        return torch.autocast(device_type="cuda", dtype=torch.float16)
    return torch.cuda.amp.autocast(dtype=torch.float16)


def _grad_scaler(torch: Any, enabled: bool) -> Any:
    if hasattr(torch, "amp") and hasattr(torch.amp, "GradScaler"):
        return torch.amp.GradScaler("cuda", enabled=enabled)
    return torch.cuda.amp.GradScaler(enabled=enabled)


def _validation_sample_limit() -> int:
    try:
        return max(0, int(os.environ.get("ALPHA_VALIDATION_SAMPLE_ROWS") or 0))
    except ValueError:
        return 0


def _validation_indices(row_count: int, limit: int) -> list[int]:
    if row_count <= 0:
        return []
    if limit <= 0 or row_count <= limit:
        return list(range(row_count))
    return np.linspace(0, row_count - 1, num=limit, dtype=np.int64).tolist()


class QlibTorchMLPModel:
    """Small deterministic MLP consuming Qlib DatasetH train/valid/test segments."""

    def __init__(
        self, *, input_dim: int, hidden_layers: list[int] | tuple[int, ...] | None = None,
        hidden_size: int | None = None, layer_count: int | None = None,
        learning_rate: float = 0.001, max_steps: int = 300, batch_size: int = 2048,
        early_stopping_rounds: int = 20, eval_steps: int = 10,
        weight_decay: float = 0.0001, seed: int = 42, num_threads: int = 4,
        loss: str = "mse",
    ) -> None:
        import torch
        from torch import nn

        self.input_dim = int(input_dim)
        if hidden_layers is None:
            # Keep direct callers of the original constructor working while all
            # newly frozen model specs use the explicit per-layer representation.
            width = int(hidden_size or 64)
            hidden_layers = [
                max(4, width // (2 ** index))
                for index in range(int(layer_count or 2))
            ]
        self.hidden_layers = tuple(int(width) for width in hidden_layers)
        if not 1 <= len(self.hidden_layers) <= 8:
            raise ValueError("hidden_layers必须包含1到8层")
        if any(width < 4 or width > 4096 for width in self.hidden_layers):
            raise ValueError("hidden_layers每层宽度必须在4到4096之间")
        self.learning_rate = float(learning_rate)
        self.max_steps = int(max_steps)
        self.requested_batch_size = int(batch_size)
        self.early_stopping_rounds = int(early_stopping_rounds)
        self.eval_steps = int(eval_steps)
        self.weight_decay = float(weight_decay)
        self.seed = int(seed)
        self.num_threads = int(num_threads)
        self.loss = str(loss)
        self.classification = self.loss == "binary"
        self.model_name = "MLP"
        # The scheduler executes jobs in a background Python thread. Reconfiguring
        # PyTorch's global CPU pool from that thread can deadlock on Apple Silicon;
        # the service process owns the pool and its environment-level limits.
        torch.manual_seed(self.seed)
        self.device = _torch_device(torch)
        if self.device.type == "cuda":
            torch.cuda.manual_seed_all(self.seed)
        self.runtime_profile = _configure_torch_runtime(torch, self.device)
        self.batch_size = _effective_torch_batch_size(
            torch, self.device, self.requested_batch_size,
        )
        self.runtime_profile.update({
            "requested_batch_size": self.requested_batch_size,
            "effective_batch_size": self.batch_size,
        })
        layers: list[Any] = []
        width = self.input_dim
        for hidden_width in self.hidden_layers:
            layers.extend((nn.Linear(width, hidden_width), nn.ReLU()))
            width = hidden_width
        layers.append(nn.Linear(width, 1))
        self.network = nn.Sequential(*layers).to(self.device)
        self.fitted = False

    def fit(
        self, dataset: Any, *, evals_result: dict[str, Any] | None = None,
        cancellation: Any = None, progress: Any = None,
    ) -> None:
        import torch
        from qlib.data.dataset import DataHandlerLP

        evaluations = evals_result if evals_result is not None else {}
        validation_enabled = bool(
            getattr(dataset, "_alphablocks_validation_enabled", True)
        )
        train, valid = dataset.prepare(
            ["train", "valid"], col_set=["feature", "label"],
            data_key=DataHandlerLP.DK_L,
        )
        x_train = torch.as_tensor(train["feature"].values, dtype=torch.float32, device=self.device)
        y_train = torch.as_tensor(train["label"].values.reshape(-1), dtype=torch.float32, device=self.device)
        x_valid = torch.as_tensor(valid["feature"].values, dtype=torch.float32, device=self.device)
        y_valid = torch.as_tensor(valid["label"].values.reshape(-1), dtype=torch.float32, device=self.device)
        if not len(x_train) or (validation_enabled and not len(x_valid)):
            raise ValueError("MLP训练集或验证集为空")
        optimizer = torch.optim.AdamW(
            self.network.parameters(), lr=self.learning_rate, weight_decay=self.weight_decay,
        )
        amp_enabled = bool(self.runtime_profile.get("amp_enabled"))
        scaler = _grad_scaler(torch, amp_enabled)
        loss_fn = (
            torch.nn.BCEWithLogitsLoss()
            if self.classification else torch.nn.MSELoss()
        )
        generator = torch.Generator().manual_seed(self.seed)
        best_loss = float("inf")
        best_state = deepcopy(self.network.state_dict())
        stale = 0
        evaluations.update({"train": [], "steps": []})
        if validation_enabled:
            evaluations["valid"] = []
        started_at = time.perf_counter()
        self.network.train()
        for step in range(1, self.max_steps + 1):
            if cancellation is not None:
                cancellation.checkpoint()
            indices = torch.randint(
                0, len(x_train), (min(self.batch_size, len(x_train)),), generator=generator,
            ).to(self.device)
            optimizer.zero_grad(set_to_none=True)
            with _autocast(torch, self.device, amp_enabled):
                output = self.network(x_train[indices]).reshape(-1)
                train_loss = loss_fn(output.float(), y_train[indices])
            scaler.scale(train_loss).backward()
            scaler.step(optimizer)
            scaler.update()
            if step % self.eval_steps != 0 and step != self.max_steps:
                continue
            evaluations["train"].append(float(train_loss.item()))
            evaluations["steps"].append(step)
            if validation_enabled:
                self.network.eval()
                with torch.no_grad():
                    with _autocast(torch, self.device, amp_enabled):
                        valid_output = self.network(x_valid).reshape(-1)
                        valid_loss = loss_fn(valid_output.float(), y_valid).item()
                evaluations["valid"].append(float(valid_loss))
            if progress is not None:
                progress(
                    "training", min(80, 58 + int(22 * step / self.max_steps)),
                    {
                        "iteration": step,
                        "total_iterations": self.max_steps,
                        "effective_batch_size": self.batch_size,
                        "amp_enabled": amp_enabled,
                        "samples_per_second": round(
                            step * self.batch_size
                            / max(0.001, time.perf_counter() - started_at),
                            1,
                        ),
                    },
                )
            if validation_enabled:
                if valid_loss + 1e-12 < best_loss:
                    best_loss = valid_loss
                    best_state = deepcopy(self.network.state_dict())
                    stale = 0
                else:
                    stale += 1
                    if stale >= self.early_stopping_rounds:
                        break
            self.network.train()
        if validation_enabled:
            self.network.load_state_dict(best_state)
        self.network.eval()
        self.fitted = True
        try:
            from qlib.workflow import R

            for step, train_loss in enumerate(evaluations["train"], start=1):
                metrics = {"train_loss": train_loss}
                if validation_enabled:
                    metrics["valid_loss"] = evaluations["valid"][step - 1]
                R.log_metrics(**metrics, step=step)
        except (AttributeError, RuntimeError):
            pass

    def predict(self, dataset: Any, segment: str = "test") -> pd.Series:
        from qlib.data.dataset import DataHandlerLP

        features = dataset.prepare(segment, col_set="feature", data_key=DataHandlerLP.DK_I)
        return pd.Series(self.predict_frame(features), index=features.index)

    def predict_frame(self, features: pd.DataFrame) -> np.ndarray:
        import torch

        if not self.fitted:
            raise ValueError("MLP模型尚未训练")
        self.network.eval()
        with torch.no_grad():
            values = torch.as_tensor(features.values, dtype=torch.float32, device=self.device)
            with _autocast(
                torch, self.device, bool(self.runtime_profile.get("amp_enabled")),
            ):
                output = self.network(values).reshape(-1)
            if self.classification:
                output = torch.sigmoid(output)
            return output.cpu().numpy().astype(float)

    def get_feature_importance(self) -> list[float]:
        first = next(layer for layer in self.network if layer.__class__.__name__ == "Linear")
        return first.weight.detach().abs().mean(dim=0).cpu().numpy().tolist()

    def to_cpu(self) -> None:
        import torch

        self.network = self.network.cpu()
        self.device = torch.device("cpu")


class QlibTorchLSTMModel:
    """Causal per-instrument sequence model backed by Qlib TSDatasetH."""

    def __init__(
        self, *, input_dim: int, lookback_window: int = 60,
        hidden_size: int = 128, num_layers: int = 2, dropout: float = 0.2,
        learning_rate: float = 0.001, max_steps: int = 300,
        batch_size: int = 512, early_stopping_rounds: int = 20,
        eval_steps: int = 10, weight_decay: float = 0.0001,
        seed: int = 42, num_threads: int = 4,
        loss: str = "mse",
    ) -> None:
        import torch
        from torch import nn

        self.input_dim = int(input_dim)
        self.lookback_window = int(lookback_window)
        self.hidden_size = int(hidden_size)
        self.num_layers = int(num_layers)
        self.dropout = float(dropout)
        self.learning_rate = float(learning_rate)
        self.max_steps = int(max_steps)
        self.requested_batch_size = int(batch_size)
        self.early_stopping_rounds = int(early_stopping_rounds)
        self.eval_steps = int(eval_steps)
        self.weight_decay = float(weight_decay)
        self.seed = int(seed)
        self.num_threads = int(num_threads)
        self.loss = str(loss)
        self.classification = self.loss == "binary"
        self.model_name = "LSTM"
        if self.lookback_window < 2:
            raise ValueError("lookback_window必须至少为2")
        torch.manual_seed(self.seed)
        self.device = _torch_device(torch)
        if self.device.type == "cuda":
            torch.cuda.manual_seed_all(self.seed)
        self.runtime_profile = _configure_torch_runtime(torch, self.device)
        self.batch_size = _effective_torch_batch_size(
            torch, self.device, self.requested_batch_size,
        )
        self.runtime_profile.update({
            "requested_batch_size": self.requested_batch_size,
            "effective_batch_size": self.batch_size,
        })
        self.encoder = nn.LSTM(
            input_size=self.input_dim,
            hidden_size=self.hidden_size,
            num_layers=self.num_layers,
            dropout=self.dropout if self.num_layers > 1 else 0.0,
            batch_first=True,
        ).to(self.device)
        self.head = nn.Linear(self.hidden_size, 1).to(self.device)
        self.fitted = False

    def _forward(self, features: Any) -> Any:
        encoded, _ = self.encoder(features)
        return self.head(encoded[:, -1, :]).reshape(-1)

    def fit(
        self, dataset: Any, *, evals_result: dict[str, Any] | None = None,
        cancellation: Any = None, progress: Any = None,
    ) -> None:
        import torch
        from qlib.data.dataset import DataHandlerLP

        evaluations = evals_result if evals_result is not None else {}
        validation_enabled = bool(
            getattr(dataset, "_alphablocks_validation_enabled", True)
        )
        train, valid = dataset.prepare(
            ["train", "valid"], col_set=["feature", "label"],
            data_key=DataHandlerLP.DK_L,
        )
        if not len(train) or (validation_enabled and not len(valid)):
            raise ValueError(f"{self.model_name}训练集或验证集为空")
        valid_indices = (
            _validation_indices(len(valid), _validation_sample_limit())
            if validation_enabled else []
        )
        valid_features, valid_labels = (
            _materialize_sequence_rows(valid, valid_indices, self.input_dim)
            if validation_enabled
            else (np.empty((0,)), np.empty((0,)))
        )
        if validation_enabled and not len(valid_features):
            raise ValueError(f"{self.model_name}验证集没有完整历史窗口")
        self.runtime_profile.update({
            "validation_rows_total": int(len(valid)) if validation_enabled else 0,
            "validation_rows_used": int(len(valid_features)),
            "validation_sampled": (
                len(valid_features) < len(valid) if validation_enabled else False
            ),
        })
        optimizer = torch.optim.AdamW(
            [*self.encoder.parameters(), *self.head.parameters()],
            lr=self.learning_rate,
            weight_decay=self.weight_decay,
        )
        loss_fn = (
            torch.nn.BCEWithLogitsLoss()
            if self.classification else torch.nn.MSELoss()
        )
        amp_enabled = bool(self.runtime_profile.get("amp_enabled"))
        scaler = _grad_scaler(torch, amp_enabled)
        generator = torch.Generator().manual_seed(self.seed)
        best_loss = float("inf")
        best_encoder = deepcopy(self.encoder.state_dict())
        best_head = deepcopy(self.head.state_dict())
        stale = 0
        successful_steps = 0
        evaluations.update({"train": [], "steps": []})
        if validation_enabled:
            evaluations["valid"] = []
        started_at = time.perf_counter()
        self.encoder.train()
        self.head.train()
        for step in range(1, self.max_steps + 1):
            if cancellation is not None:
                cancellation.checkpoint()
            indices = torch.randint(
                0, len(train), (min(self.batch_size, len(train)),), generator=generator,
            ).tolist()
            features, labels, _ = _sequence_batch(train, indices, self.input_dim, with_label=True)
            if not len(features):
                continue
            successful_steps += 1
            x_train = torch.as_tensor(features, dtype=torch.float32, device=self.device)
            y_train = torch.as_tensor(labels, dtype=torch.float32, device=self.device)
            optimizer.zero_grad(set_to_none=True)
            with _autocast(torch, self.device, amp_enabled):
                output = self._forward(x_train)
                train_loss = loss_fn(output.float(), y_train)
            scaler.scale(train_loss).backward()
            scaler.step(optimizer)
            scaler.update()
            if step % self.eval_steps != 0 and step != self.max_steps:
                continue
            evaluations["train"].append(float(train_loss.item()))
            evaluations["steps"].append(step)
            if validation_enabled:
                valid_loss = self._validation_loss(
                    valid_features, valid_labels, loss_fn, cancellation,
                )
                evaluations["valid"].append(float(valid_loss))
            if progress is not None:
                progress(
                    "training", min(80, 58 + int(22 * step / self.max_steps)),
                    {
                        "iteration": step,
                        "total_iterations": self.max_steps,
                        "effective_batch_size": self.batch_size,
                        "amp_enabled": amp_enabled,
                        "validation_rows": len(valid_features),
                        "samples_per_second": round(
                            successful_steps * self.batch_size
                            / max(0.001, time.perf_counter() - started_at),
                            1,
                        ),
                    },
                )
            if validation_enabled:
                if valid_loss + 1e-12 < best_loss:
                    best_loss = valid_loss
                    best_encoder = deepcopy(self.encoder.state_dict())
                    best_head = deepcopy(self.head.state_dict())
                    stale = 0
                else:
                    stale += 1
                    if stale >= self.early_stopping_rounds:
                        break
            self.encoder.train()
            self.head.train()
        if validation_enabled and successful_steps and not np.isfinite(best_loss):
            valid_loss = self._validation_loss(
                valid_features, valid_labels, loss_fn, cancellation,
            )
            evaluations["train"].append(float(train_loss.item()))
            evaluations["valid"].append(float(valid_loss))
            evaluations["steps"].append(self.max_steps)
            best_loss = valid_loss
            best_encoder = deepcopy(self.encoder.state_dict())
            best_head = deepcopy(self.head.state_dict())
        if successful_steps == 0 or (
            validation_enabled and not np.isfinite(best_loss)
        ):
            raise ValueError(f"{self.model_name}没有可用的完整历史窗口样本")
        if validation_enabled:
            self.encoder.load_state_dict(best_encoder)
            self.head.load_state_dict(best_head)
        self.encoder.eval()
        self.head.eval()
        self.fitted = True
        try:
            from qlib.workflow import R

            for step, train_loss in enumerate(evaluations["train"], start=1):
                metrics = {"train_loss": train_loss}
                if validation_enabled:
                    metrics["valid_loss"] = evaluations["valid"][step - 1]
                R.log_metrics(**metrics, step=step)
        except (AttributeError, RuntimeError):
            pass

    def _validation_loss(
        self, features: np.ndarray, labels: np.ndarray,
        loss_fn: Any, cancellation: Any,
    ) -> float:
        import torch

        total_loss = 0.0
        total_rows = 0
        self.encoder.eval()
        self.head.eval()
        amp_enabled = bool(self.runtime_profile.get("amp_enabled"))
        with torch.no_grad():
            for start in range(0, len(features), self.batch_size):
                if cancellation is not None:
                    cancellation.checkpoint()
                end = min(len(features), start + self.batch_size)
                batch_features = torch.as_tensor(
                    features[start:end], dtype=torch.float32, device=self.device,
                )
                target = torch.as_tensor(
                    labels[start:end], dtype=torch.float32, device=self.device,
                )
                with _autocast(torch, self.device, amp_enabled):
                    values = self._forward(batch_features)
                    loss = float(loss_fn(values.float(), target).item())
                total_loss += loss * (end - start)
                total_rows += end - start
        if not total_rows:
            raise ValueError(f"{self.model_name}验证集没有完整历史窗口")
        return total_loss / total_rows

    def predict(self, dataset: Any, segment: str = "test") -> pd.Series:
        from qlib.data.dataset import DataHandlerLP

        sampler = dataset.prepare(
            segment, col_set="feature", data_key=DataHandlerLP.DK_I,
        )
        values, positions = self.predict_sampler(sampler)
        index = sampler.get_index().take(positions)
        return pd.Series(values, index=index).sort_index()

    def predict_sampled(
        self, dataset: Any, segment: str, *, max_rows: int,
    ) -> pd.Series:
        """Predict a deterministic segment sample for diagnostic-only metrics."""
        from qlib.data.dataset import DataHandlerLP

        sampler = dataset.prepare(
            segment, col_set="feature", data_key=DataHandlerLP.DK_I,
        )
        selected_positions = _validation_indices(len(sampler), max_rows)
        values, positions = self.predict_sampler(
            sampler, selected_positions=selected_positions,
        )
        self.runtime_profile.update({
            "train_metric_rows_total": int(len(sampler)),
            "train_metric_rows_used": int(len(positions)),
            "train_metric_sampled": len(positions) < len(sampler),
        })
        index = sampler.get_index().take(positions)
        return pd.Series(values, index=index).sort_index()

    def predict_sampler(
        self, sampler: Any, *, selected_positions: list[int] | None = None,
    ) -> tuple[np.ndarray, list[int]]:
        import torch

        if not self.fitted:
            raise ValueError(f"{self.model_name}模型尚未训练")
        predictions: list[np.ndarray] = []
        positions: list[int] = []
        self.encoder.eval()
        self.head.eval()
        amp_enabled = bool(self.runtime_profile.get("amp_enabled"))
        selected_count = (
            len(selected_positions) if selected_positions is not None else len(sampler)
        )
        with torch.no_grad():
            for start in range(0, selected_count, self.batch_size):
                if selected_positions is None:
                    indices = list(range(
                        start, min(len(sampler), start + self.batch_size),
                    ))
                else:
                    indices = selected_positions[start:start + self.batch_size]
                features, _, valid_positions = _sequence_batch(
                    sampler, indices, self.input_dim, with_label=False,
                )
                if not len(features):
                    continue
                with _autocast(torch, self.device, amp_enabled):
                    output = self._forward(
                        torch.as_tensor(
                            features, dtype=torch.float32, device=self.device,
                        ),
                    )
                if self.classification:
                    output = torch.sigmoid(output)
                output = output.cpu().numpy().astype(float)
                predictions.append(output)
                positions.extend(valid_positions)
        if not predictions:
            raise ValueError(f"{self.model_name}推理没有完整历史窗口")
        return np.concatenate(predictions), positions

    def get_feature_importance(self) -> list[float]:
        weights = self.encoder.weight_ih_l0.detach().abs().mean(dim=0)
        return weights.cpu().numpy().tolist()

    def to_cpu(self) -> None:
        import torch

        self.encoder = self.encoder.cpu()
        self.head = self.head.cpu()
        self.device = torch.device("cpu")


class QlibTorchTransformerLSTMModel(QlibTorchLSTMModel):
    """Causal Transformer encoder followed by an LSTM sequence head."""

    def __init__(
        self, *, input_dim: int, lookback_window: int = 60,
        d_model: int = 64, nhead: int = 4, transformer_layers: int = 2,
        dim_feedforward: int = 256, lstm_hidden_size: int = 128,
        lstm_layers: int = 1, dropout: float = 0.2,
        learning_rate: float = 0.001, max_steps: int = 300,
        batch_size: int = 256, early_stopping_rounds: int = 20,
        eval_steps: int = 10, weight_decay: float = 0.0001,
        seed: int = 42, num_threads: int = 4,
        loss: str = "mse",
    ) -> None:
        import torch
        from torch import nn

        if int(d_model) % int(nhead) != 0:
            raise ValueError("d_model必须能被nhead整除")
        super().__init__(
            input_dim=input_dim,
            lookback_window=lookback_window,
            hidden_size=lstm_hidden_size,
            num_layers=lstm_layers,
            dropout=dropout,
            learning_rate=learning_rate,
            max_steps=max_steps,
            batch_size=batch_size,
            early_stopping_rounds=early_stopping_rounds,
            eval_steps=eval_steps,
            weight_decay=weight_decay,
            seed=seed,
            num_threads=num_threads,
            loss=loss,
        )
        self.model_name = "Transformer+LSTM"
        self.d_model = int(d_model)
        self.nhead = int(nhead)
        self.transformer_layers = int(transformer_layers)
        self.dim_feedforward = int(dim_feedforward)
        self.lstm_hidden_size = int(lstm_hidden_size)
        self.lstm_layers = int(lstm_layers)
        torch.manual_seed(self.seed)
        layer = nn.TransformerEncoderLayer(
            d_model=self.d_model,
            nhead=self.nhead,
            dim_feedforward=self.dim_feedforward,
            dropout=self.dropout,
            activation="gelu",
            batch_first=True,
            norm_first=False,
        )
        # ModuleDict keeps the complete hybrid encoder pickle-safe while the
        # inherited training loop can optimize and checkpoint it as one unit.
        self.encoder = nn.ModuleDict({
            "input_projection": nn.Linear(self.input_dim, self.d_model),
            "position_embedding": nn.Embedding(self.lookback_window, self.d_model),
            "transformer": nn.TransformerEncoder(layer, num_layers=self.transformer_layers),
            "lstm": nn.LSTM(
                input_size=self.d_model,
                hidden_size=self.lstm_hidden_size,
                num_layers=self.lstm_layers,
                dropout=self.dropout if self.lstm_layers > 1 else 0.0,
                batch_first=True,
            ),
        }).to(self.device)
        self.head = nn.Linear(self.lstm_hidden_size, 1).to(self.device)
        self.fitted = False

    def _forward(self, features: Any) -> Any:
        import torch

        steps = int(features.shape[1])
        if steps > self.lookback_window:
            raise ValueError("输入序列长度超过lookback_window")
        positions = torch.arange(steps, device=features.device)
        encoded = self.encoder["input_projection"](features)
        encoded = encoded + self.encoder["position_embedding"](positions).unsqueeze(0)
        causal_mask = torch.triu(
            torch.full((steps, steps), float("-inf"), device=features.device), diagonal=1,
        )
        encoded = self.encoder["transformer"](encoded, mask=causal_mask)
        encoded, _ = self.encoder["lstm"](encoded)
        return self.head(encoded[:, -1, :]).reshape(-1)

    def get_feature_importance(self) -> list[float]:
        projection = self.encoder["input_projection"]
        return projection.weight.detach().abs().mean(dim=0).cpu().numpy().tolist()


_SEQUENCE_BASE_PARAMS = frozenset({
    "input_dim", "lookback_window", "hidden_size", "num_layers", "dropout",
    "learning_rate", "max_steps", "batch_size", "early_stopping_rounds",
    "eval_steps", "weight_decay", "seed", "num_threads", "loss",
})


def _sequence_base_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in kwargs.items()
        if key in _SEQUENCE_BASE_PARAMS
    }


class QlibTorchGRUModel(QlibTorchLSTMModel):
    """门控循环单元，训练循环与LSTM完全一致，编码器替换为GRU。"""

    def __init__(self, **kwargs: Any) -> None:
        import torch
        from torch import nn

        super().__init__(**_sequence_base_kwargs(kwargs))
        self.model_name = "GRU"
        torch.manual_seed(self.seed)
        self.encoder = nn.GRU(
            input_size=self.input_dim,
            hidden_size=self.hidden_size,
            num_layers=self.num_layers,
            dropout=self.dropout if self.num_layers > 1 else 0.0,
            batch_first=True,
        ).to(self.device)
        self.head = nn.Linear(self.hidden_size, 1).to(self.device)
        self.fitted = False


class QlibTorchALSTMModel(QlibTorchLSTMModel):
    """带加性注意力的LSTM：注意力在隐藏状态序列上加权得到上下文向量。"""

    def __init__(self, **kwargs: Any) -> None:
        import torch
        from torch import nn

        super().__init__(**_sequence_base_kwargs(kwargs))
        self.model_name = "ALSTM"
        torch.manual_seed(self.seed)
        self.encoder = nn.ModuleDict({
            "lstm": nn.LSTM(
                input_size=self.input_dim,
                hidden_size=self.hidden_size,
                num_layers=self.num_layers,
                dropout=self.dropout if self.num_layers > 1 else 0.0,
                batch_first=True,
            ),
            "attn_proj": nn.Linear(self.hidden_size, self.hidden_size),
            "attn_last": nn.Linear(self.hidden_size, self.hidden_size),
            "attn_v": nn.Linear(self.hidden_size, 1),
        }).to(self.device)
        self.head = nn.Linear(self.hidden_size, 1).to(self.device)
        self.fitted = False

    def _forward(self, features: Any) -> Any:
        import torch

        encoded, _ = self.encoder["lstm"](features)
        last = encoded[:, -1:, :]
        scores = self.encoder["attn_v"](torch.tanh(
            self.encoder["attn_proj"](encoded) + self.encoder["attn_last"](last),
        ))
        attention = torch.softmax(scores, dim=1)
        context = (attention * encoded).sum(dim=1)
        return self.head(context).reshape(-1)

    def get_feature_importance(self) -> list[float]:
        weights = self.encoder["lstm"].weight_ih_l0.detach().abs().mean(dim=0)
        return weights.cpu().numpy().tolist()


class QlibTorchTransformerModel(QlibTorchLSTMModel):
    """因果Transformer编码器，输出最后一个时间步。"""

    def __init__(self, **kwargs: Any) -> None:
        import torch
        from torch import nn

        d_model = int(kwargs.get("d_model", 64))
        nhead = int(kwargs.get("nhead", 4))
        if d_model % nhead != 0:
            raise ValueError("d_model必须能被nhead整除")
        transformer_layers = int(kwargs.get("transformer_layers", 2))
        dim_feedforward = int(kwargs.get("dim_feedforward", 256))
        dropout = float(kwargs.get("dropout", 0.2))
        base_kwargs = _sequence_base_kwargs(kwargs)
        base_kwargs.update({
            "hidden_size": d_model,
            "num_layers": 1,
            "dropout": dropout,
        })
        super().__init__(**base_kwargs)
        self.model_name = "Transformer"
        self.d_model = d_model
        self.nhead = nhead
        self.transformer_layers = transformer_layers
        self.dim_feedforward = dim_feedforward
        torch.manual_seed(self.seed)
        layer = nn.TransformerEncoderLayer(
            d_model=self.d_model,
            nhead=self.nhead,
            dim_feedforward=self.dim_feedforward,
            dropout=self.dropout,
            activation="gelu",
            batch_first=True,
            norm_first=False,
        )
        self.encoder = nn.ModuleDict({
            "input_projection": nn.Linear(self.input_dim, self.d_model),
            "position_embedding": nn.Embedding(self.lookback_window, self.d_model),
            "transformer": nn.TransformerEncoder(
                layer, num_layers=self.transformer_layers,
            ),
        }).to(self.device)
        self.head = nn.Linear(self.d_model, 1).to(self.device)
        self.fitted = False

    def _forward(self, features: Any) -> Any:
        import torch

        steps = int(features.shape[1])
        if steps > self.lookback_window:
            raise ValueError("输入序列长度超过lookback_window")
        positions = torch.arange(steps, device=features.device)
        encoded = self.encoder["input_projection"](features)
        encoded = encoded + self.encoder["position_embedding"](positions).unsqueeze(0)
        causal_mask = torch.triu(
            torch.full((steps, steps), float("-inf"), device=features.device),
            diagonal=1,
        )
        encoded = self.encoder["transformer"](encoded, mask=causal_mask)
        return self.head(encoded[:, -1, :]).reshape(-1)

    def get_feature_importance(self) -> list[float]:
        projection = self.encoder["input_projection"]
        return projection.weight.detach().abs().mean(dim=0).cpu().numpy().tolist()


try:
    import torch as _torch
except ImportError:  # pragma: no cover - torch缺失时仅树/线性模型可用
    _torch = None


if _torch is not None:
    class _CausalConv1d(_torch.nn.Module):
        """因果膨胀卷积：右侧输出裁剪保证t时刻只使用<=t的输入。"""

        def __init__(
            self, in_channels: int, out_channels: int,
            kernel_size: int, dilation: int,
        ) -> None:
            super().__init__()
            self.conv = _torch.nn.Conv1d(
                in_channels, out_channels, kernel_size,
                dilation=dilation,
                padding=(kernel_size - 1) * dilation,
            )

        def forward(self, x: Any) -> Any:
            length = int(x.shape[-1])
            return self.conv(x)[:, :, :length]


    class _TCNResidualBlock(_torch.nn.Module):
        def __init__(
            self, channels: int, kernel_size: int,
            dilation: int, dropout: float,
        ) -> None:
            super().__init__()
            self.conv1 = _CausalConv1d(channels, channels, kernel_size, dilation)
            self.conv2 = _CausalConv1d(channels, channels, kernel_size, dilation)
            self.activation = _torch.nn.ReLU()
            self.dropout = _torch.nn.Dropout(dropout)

        def forward(self, x: Any) -> Any:
            out = self.dropout(self.activation(self.conv1(x)))
            out = self.dropout(self.activation(self.conv2(out)))
            return x + out


class QlibTorchTCNModel(QlibTorchLSTMModel):
    """时间卷积网络：膨胀因果卷积 + 残差连接，输出最后一个时间步。"""

    def __init__(self, **kwargs: Any) -> None:
        import torch
        from torch import nn

        kernel_size = int(kwargs.get("kernel_size", 5))
        num_layers = int(kwargs.get("num_layers", 5))
        dropout = float(kwargs.get("dropout", 0.5))
        base_kwargs = _sequence_base_kwargs(kwargs)
        base_kwargs.update({
            "num_layers": 1,
            "dropout": dropout,
        })
        super().__init__(**base_kwargs)
        self.model_name = "TCN"
        self.kernel_size = kernel_size
        self.tcn_layers = num_layers
        torch.manual_seed(self.seed)
        self.encoder = nn.ModuleDict({
            "stem": nn.Conv1d(self.input_dim, self.hidden_size, kernel_size=1),
            "blocks": nn.ModuleList([
                _TCNResidualBlock(
                    self.hidden_size, self.kernel_size,
                    dilation=2 ** index, dropout=self.dropout,
                )
                for index in range(self.tcn_layers)
            ]),
        }).to(self.device)
        self.head = nn.Linear(self.hidden_size, 1).to(self.device)
        self.fitted = False

    def _forward(self, features: Any) -> Any:
        encoded = features.transpose(1, 2)
        encoded = self.encoder["stem"](encoded)
        for block in self.encoder["blocks"]:
            encoded = block(encoded)
        encoded = encoded.transpose(1, 2)
        return self.head(encoded[:, -1, :]).reshape(-1)

    def get_feature_importance(self) -> list[float]:
        weights = self.encoder["stem"].weight.detach().abs().mean(dim=(0, 2))
        return weights.cpu().numpy().tolist()


class QlibTorchNativeTFTModel(QlibTorchLSTMModel):
    """轻量TFT变体：GRU编码 + 前馈 + 因果多头注意力 + 门控残差。"""

    def __init__(self, **kwargs: Any) -> None:
        import torch
        from torch import nn

        d_model = int(kwargs.get("d_model", 64))
        nhead = int(kwargs.get("nhead", 4))
        if d_model % nhead != 0:
            raise ValueError("d_model必须能被nhead整除")
        gru_hidden_size = int(kwargs.get("gru_hidden_size", 64))
        num_layers = int(kwargs.get("num_layers", 1))
        dim_feedforward = int(kwargs.get("dim_feedforward", 128))
        dropout = float(kwargs.get("dropout", 0.2))
        base_kwargs = _sequence_base_kwargs(kwargs)
        base_kwargs.update({
            "hidden_size": gru_hidden_size,
            "num_layers": num_layers,
            "dropout": dropout,
        })
        super().__init__(**base_kwargs)
        self.model_name = "NativeTFT"
        self.d_model = d_model
        self.nhead = nhead
        self.gru_hidden_size = gru_hidden_size
        self.gru_layers = num_layers
        self.dim_feedforward = dim_feedforward
        torch.manual_seed(self.seed)
        self.encoder = nn.ModuleDict({
            "input_projection": nn.Linear(self.input_dim, self.d_model),
            "position_embedding": nn.Embedding(self.lookback_window, self.d_model),
            "gru": nn.GRU(
                input_size=self.d_model,
                hidden_size=self.gru_hidden_size,
                num_layers=self.gru_layers,
                dropout=self.dropout if self.gru_layers > 1 else 0.0,
                batch_first=True,
            ),
            "feedforward": nn.Sequential(
                nn.Linear(self.gru_hidden_size, self.dim_feedforward),
                nn.GELU(),
                nn.Linear(self.dim_feedforward, self.d_model),
            ),
            "attention": nn.MultiheadAttention(
                self.d_model, self.nhead, dropout=self.dropout, batch_first=True,
            ),
            "gate": nn.Sequential(
                nn.Linear(self.d_model, self.d_model), nn.Sigmoid(),
            ),
        }).to(self.device)
        self.head = nn.Linear(self.d_model, 1).to(self.device)
        self.fitted = False

    def _forward(self, features: Any) -> Any:
        import torch

        steps = int(features.shape[1])
        if steps > self.lookback_window:
            raise ValueError("输入序列长度超过lookback_window")
        positions = torch.arange(steps, device=features.device)
        sequence = self.encoder["input_projection"](features)
        sequence = sequence + self.encoder["position_embedding"](positions).unsqueeze(0)
        gru_out, _ = self.encoder["gru"](sequence)
        projected = self.encoder["feedforward"](gru_out)
        causal_mask = torch.triu(
            torch.full((steps, steps), float("-inf"), device=features.device),
            diagonal=1,
        )
        attended, _ = self.encoder["attention"](
            projected, projected, projected, attn_mask=causal_mask,
        )
        gated = self.encoder["gate"](attended) * attended
        return self.head((gated + sequence)[:, -1, :]).reshape(-1)

    def get_feature_importance(self) -> list[float]:
        projection = self.encoder["input_projection"]
        return projection.weight.detach().abs().mean(dim=0).cpu().numpy().tolist()


class _QlibSklearnMixin:
    """sklearn估计器的Qlib数据集适配：fit/predict遵循Qlib Model接口。"""

    def _predict_values(self, features: Any) -> np.ndarray:
        if getattr(self, "classification", False) and hasattr(self.model, "predict_proba"):
            probabilities = np.asarray(self.model.predict_proba(features), dtype=float)
            return probabilities[:, 1]
        return np.asarray(self.model.predict(features), dtype=float).reshape(-1)

    def fit(
        self, dataset: Any, *, evals_result: dict[str, Any] | None = None,
        cancellation: Any = None, progress: Any = None,
    ) -> None:
        from qlib.data.dataset import DataHandlerLP

        evaluations = evals_result if evals_result is not None else {}
        validation_enabled = bool(
            getattr(dataset, "_alphablocks_validation_enabled", True)
        )
        if cancellation is not None:
            cancellation.checkpoint()
        train, valid = dataset.prepare(
            ["train", "valid"], col_set=["feature", "label"],
            data_key=DataHandlerLP.DK_L,
        )
        train = train.dropna()
        valid = valid.dropna()
        if train.empty or (validation_enabled and valid.empty):
            raise ValueError(f"{self.model_name}训练集或验证集为空")
        x_train = np.asarray(train["feature"].values, dtype=float)
        y_train = np.asarray(train["label"].values, dtype=float).reshape(-1)
        self.model.fit(x_train, y_train)
        train_prediction = self._predict_values(x_train)
        train_loss = float(np.sqrt(np.mean(np.square(train_prediction - y_train))))
        evaluations.update({"train": [train_loss], "steps": [1]})
        if validation_enabled:
            x_valid = np.asarray(valid["feature"].values, dtype=float)
            y_valid = np.asarray(valid["label"].values, dtype=float).reshape(-1)
            prediction = self._predict_values(x_valid)
            valid_loss = float(np.sqrt(np.mean(np.square(prediction - y_valid))))
            evaluations["valid"] = [valid_loss]
        self.fitted = True
        if progress is not None:
            progress("training", 80, {"iteration": 1, "total_iterations": 1})
        try:
            from qlib.workflow import R

            metrics = {"train_loss": train_loss}
            if validation_enabled:
                metrics["valid_loss"] = valid_loss
            R.log_metrics(**metrics, step=1)
        except (AttributeError, RuntimeError):
            pass

    def predict(self, dataset: Any, segment: str = "test") -> pd.Series:
        from qlib.data.dataset import DataHandlerLP

        frame = dataset.prepare(
            segment, col_set="feature", data_key=DataHandlerLP.DK_I,
        )
        values = np.asarray(frame.values, dtype=float)
        predictions = self._predict_values(values)
        return pd.Series(predictions, index=frame.index).sort_index()

    def to_cpu(self) -> None:
        return None


class QlibSklearnRidgeModel(_QlibSklearnMixin):
    """Ridge回归或Logistic分类基线，sanity check用。"""

    def __init__(
        self, *, input_dim: int, alpha: float = 1.0,
        fit_intercept: bool = True, solver: str = "auto",
        max_iter: int = 1000, seed: int = 42, num_threads: int = 4,
        loss: str = "mse",
    ) -> None:
        from sklearn.linear_model import LogisticRegression, Ridge

        self.input_dim = int(input_dim)
        self.seed = int(seed)
        self.num_threads = int(num_threads)
        self.loss = str(loss)
        self.classification = self.loss == "binary"
        if self.classification:
            regularization = max(float(alpha), 1e-12)
            self.model = LogisticRegression(
                C=1.0 / regularization,
                fit_intercept=bool(fit_intercept),
                solver="lbfgs",
                max_iter=int(max_iter),
                random_state=int(seed),
            )
            self.model_name = "LogisticRegression"
        else:
            self.model = Ridge(
                alpha=float(alpha),
                fit_intercept=bool(fit_intercept),
                solver=str(solver),
                max_iter=int(max_iter),
                random_state=int(seed),
            )
            self.model_name = "Ridge"
        self.fitted = False

    def get_feature_importance(self) -> list[float]:
        coefficients = np.asarray(self.model.coef_, dtype=float).reshape(-1)
        return np.abs(coefficients).tolist()


class QlibSklearnRandomForestModel(_QlibSklearnMixin):
    """随机森林回归/分类基线，用于与Boosting模型对比。"""

    def __init__(
        self, *, input_dim: int, n_estimators: int = 300,
        max_depth: int = 0, min_samples_split: int = 2,
        min_samples_leaf: int = 1, max_features: float | str = "sqrt",
        seed: int = 42, num_threads: int = 4,
        loss: str = "mse",
    ) -> None:
        from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor

        self.input_dim = int(input_dim)
        self.seed = int(seed)
        self.num_threads = int(num_threads)
        self.loss = str(loss)
        self.classification = self.loss == "binary"
        depth = int(max_depth)
        estimator_class = RandomForestClassifier if self.classification else RandomForestRegressor
        self.model = estimator_class(
            n_estimators=int(n_estimators),
            max_depth=None if depth <= 0 else depth,
            min_samples_split=int(min_samples_split),
            min_samples_leaf=int(min_samples_leaf),
            max_features=max_features,
            n_jobs=int(num_threads),
            random_state=int(seed),
            verbose=0,
        )
        self.model_name = "RandomForestClassifier" if self.classification else "RandomForest"
        self.fitted = False

    def get_feature_importance(self) -> list[float]:
        return np.asarray(self.model.feature_importances_, dtype=float).tolist()


class QlibNativeTabNetAdapter:
    """Qlib TabNet适配：统一取消/进度接口，并规避小样本下空验证批导致的崩溃。"""

    def __init__(self, *, input_dim: int, **kwargs: Any) -> None:
        import torch

        self.input_dim = int(input_dim)
        self.kwargs = dict(kwargs)
        self.loss = str(self.kwargs.pop("loss", "mse"))
        self.classification = self.loss == "binary"
        self.seed = int(self.kwargs.get("seed", 42))
        self.device = _torch_device(torch)
        self.model = None
        self.model_name = "TabNet"
        self.fitted = False

    def fit(
        self, dataset: Any, *, evals_result: dict[str, Any] | None = None,
        cancellation: Any = None, progress: Any = None,
    ) -> None:
        import torch
        from qlib.contrib.model.pytorch_tabnet import TabnetModel
        from qlib.data.dataset import DataHandlerLP

        evaluations = evals_result if evals_result is not None else {}
        validation_enabled = bool(
            getattr(dataset, "_alphablocks_validation_enabled", True)
        )
        if cancellation is not None:
            cancellation.checkpoint()
        train, valid = dataset.prepare(
            ["train", "valid"], col_set=["feature", "label"],
            data_key=DataHandlerLP.DK_L,
        )
        if train.empty or (validation_enabled and valid.empty):
            raise ValueError("TabNet训练集或验证集为空")
        batch_size = int(self.kwargs.get("batch_size", 4096))
        # qlib的test_epoch会丢弃最后一个不足batch_size的批次，小样本下会得到
        # 空指标并触发best_param未初始化错误，这里把batch钳制到样本量以内。
        batch_size = max(1, min(batch_size, len(train), len(valid)))
        torch.manual_seed(self.seed)
        model_kwargs = {**self.kwargs, "loss": "mse", "batch_size": batch_size, "GPU": -1}
        if not validation_enabled:
            model_kwargs["early_stop"] = int(model_kwargs.get("n_epochs", 200)) + 1
        self.model = TabnetModel(
            d_feat=self.input_dim,
            **model_kwargs,
        )
        self.model.fit(dataset, evals_result=evaluations)
        if not validation_enabled:
            evaluations.pop("valid", None)
        self.fitted = True
        if progress is not None:
            progress("training", 80, {"iteration": 1, "total_iterations": 1})
        try:
            from qlib.workflow import R

            for step, (train_loss, valid_loss) in enumerate(
                zip(evaluations.get("train", []), evaluations.get("valid", [])),
                start=1,
            ):
                R.log_metrics(train_loss=train_loss, valid_loss=valid_loss, step=step)
        except (AttributeError, RuntimeError):
            pass

    def predict(self, dataset: Any, segment: str = "test") -> pd.Series:
        if self.model is None or not self.fitted:
            raise ValueError("TabNet模型尚未训练")
        prediction = self.model.predict(dataset, segment)
        if self.classification:
            return prediction.clip(lower=0.0, upper=1.0)
        return prediction

    def to_cpu(self) -> None:
        return None


def _materialize_sequence_rows(
    sampler: Any, indices: list[int], input_dim: int,
    *, chunk_size: int = 8192,
) -> tuple[np.ndarray, np.ndarray]:
    feature_chunks: list[np.ndarray] = []
    label_chunks: list[np.ndarray] = []
    for start in range(0, len(indices), chunk_size):
        features, labels, _ = _sequence_batch(
            sampler, indices[start:start + chunk_size], input_dim, with_label=True,
        )
        if len(features):
            feature_chunks.append(features)
            label_chunks.append(labels)
    if not feature_chunks:
        return (
            np.empty((0, 0, input_dim), dtype=np.float32),
            np.empty((0,), dtype=np.float32),
        )
    return np.concatenate(feature_chunks), np.concatenate(label_chunks)


def _sequence_batch(
    sampler: Any,
    indices: list[int],
    input_dim: int,
    *,
    with_label: bool,
) -> tuple[np.ndarray, np.ndarray, list[int]]:
    if not indices:
        return (
            np.empty((0, 0, input_dim), dtype=np.float32),
            np.empty((0,), dtype=np.float32),
            [],
        )
    values = np.asarray(sampler[indices], dtype=np.float32)
    features = values[:, :, :input_dim]
    valid = np.isfinite(features).all(axis=(1, 2))
    labels = np.empty((len(values),), dtype=np.float32)
    if with_label:
        labels = values[:, -1, input_dim]
        valid &= np.isfinite(labels)
    valid_indices = np.flatnonzero(valid)
    return (
        features[valid_indices],
        labels[valid_indices],
        [indices[int(position)] for position in valid_indices],
    )


def _torch_device(torch: Any) -> Any:
    requested = str(os.environ.get("ALPHA_TORCH_DEVICE") or "auto").strip().lower()
    if requested == "cuda":
        if not torch.cuda.is_available():
            raise RuntimeError("远程训练请求CUDA，但当前PyTorch无法访问GPU")
        return torch.device("cuda")
    if requested not in {"", "auto", "cpu"}:
        raise ValueError("ALPHA_TORCH_DEVICE只允许auto、cpu或cuda")
    if requested == "auto" and torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


__all__ = [
    "QlibNativeTabNetAdapter", "QlibSklearnRandomForestModel", "QlibSklearnRidgeModel",
    "QlibTorchALSTMModel", "QlibTorchGRUModel", "QlibTorchLSTMModel",
    "QlibTorchMLPModel", "QlibTorchNativeTFTModel", "QlibTorchTCNModel",
    "QlibTorchTransformerLSTMModel", "QlibTorchTransformerModel",
]
