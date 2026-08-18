from __future__ import annotations

from copy import deepcopy
import os
from typing import Any

import numpy as np
import pandas as pd


class QlibTorchMLPModel:
    """Small deterministic MLP consuming Qlib DatasetH train/valid/test segments."""

    def __init__(
        self, *, input_dim: int, hidden_layers: list[int] | tuple[int, ...] | None = None,
        hidden_size: int | None = None, layer_count: int | None = None,
        learning_rate: float = 0.001, max_steps: int = 300, batch_size: int = 2048,
        early_stopping_rounds: int = 10, eval_steps: int = 10,
        weight_decay: float = 0.0001, seed: int = 42, num_threads: int = 4,
    ) -> None:
        import torch
        from torch import nn

        self.input_dim = int(input_dim)
        if hidden_layers is None:
            # Keep direct callers of the original constructor working while all
            # newly frozen model specs use the explicit per-layer representation.
            width = int(hidden_size or 64)
            hidden_layers = [width] * int(layer_count or 2)
        self.hidden_layers = tuple(int(width) for width in hidden_layers)
        if not 1 <= len(self.hidden_layers) <= 8:
            raise ValueError("hidden_layers必须包含1到8层")
        if any(width < 4 or width > 4096 for width in self.hidden_layers):
            raise ValueError("hidden_layers每层宽度必须在4到4096之间")
        self.learning_rate = float(learning_rate)
        self.max_steps = int(max_steps)
        self.batch_size = int(batch_size)
        self.early_stopping_rounds = int(early_stopping_rounds)
        self.eval_steps = int(eval_steps)
        self.weight_decay = float(weight_decay)
        self.seed = int(seed)
        self.num_threads = int(num_threads)
        self.model_name = "MLP"
        # The scheduler executes jobs in a background Python thread. Reconfiguring
        # PyTorch's global CPU pool from that thread can deadlock on Apple Silicon;
        # the service process owns the pool and its environment-level limits.
        torch.manual_seed(self.seed)
        self.device = _torch_device(torch)
        if self.device.type == "cuda":
            torch.cuda.manual_seed_all(self.seed)
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
        train, valid = dataset.prepare(
            ["train", "valid"], col_set=["feature", "label"],
            data_key=DataHandlerLP.DK_L,
        )
        x_train = torch.as_tensor(train["feature"].values, dtype=torch.float32, device=self.device)
        y_train = torch.as_tensor(train["label"].values.reshape(-1), dtype=torch.float32, device=self.device)
        x_valid = torch.as_tensor(valid["feature"].values, dtype=torch.float32, device=self.device)
        y_valid = torch.as_tensor(valid["label"].values.reshape(-1), dtype=torch.float32, device=self.device)
        if not len(x_train) or not len(x_valid):
            raise ValueError("MLP训练集或验证集为空")
        optimizer = torch.optim.AdamW(
            self.network.parameters(), lr=self.learning_rate, weight_decay=self.weight_decay,
        )
        loss_fn = torch.nn.MSELoss()
        generator = torch.Generator().manual_seed(self.seed)
        best_loss = float("inf")
        best_state = deepcopy(self.network.state_dict())
        stale = 0
        evaluations.update({"train": [], "valid": [], "steps": []})
        self.network.train()
        for step in range(1, self.max_steps + 1):
            if cancellation is not None:
                cancellation.checkpoint()
            indices = torch.randint(
                0, len(x_train), (min(self.batch_size, len(x_train)),), generator=generator,
            ).to(self.device)
            optimizer.zero_grad(set_to_none=True)
            train_loss = loss_fn(self.network(x_train[indices]).reshape(-1), y_train[indices])
            train_loss.backward()
            optimizer.step()
            if step % self.eval_steps != 0 and step != self.max_steps:
                continue
            self.network.eval()
            with torch.no_grad():
                valid_loss = loss_fn(self.network(x_valid).reshape(-1), y_valid).item()
            evaluations["train"].append(float(train_loss.item()))
            evaluations["valid"].append(float(valid_loss))
            evaluations["steps"].append(step)
            if progress is not None:
                progress(
                    "training", min(80, 58 + int(22 * step / self.max_steps)),
                    {"iteration": step, "total_iterations": self.max_steps},
                )
            if valid_loss + 1e-12 < best_loss:
                best_loss = valid_loss
                best_state = deepcopy(self.network.state_dict())
                stale = 0
            else:
                stale += 1
                if stale >= self.early_stopping_rounds:
                    break
            self.network.train()
        self.network.load_state_dict(best_state)
        self.network.eval()
        self.fitted = True
        try:
            from qlib.workflow import R

            for step, (train_loss, valid_loss) in enumerate(
                zip(evaluations["train"], evaluations["valid"]), start=1,
            ):
                R.log_metrics(train_loss=train_loss, valid_loss=valid_loss, step=step)
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
            return self.network(values).reshape(-1).cpu().numpy().astype(float)

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
        batch_size: int = 512, early_stopping_rounds: int = 10,
        eval_steps: int = 10, weight_decay: float = 0.0001,
        seed: int = 42, num_threads: int = 4,
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
        self.batch_size = int(batch_size)
        self.early_stopping_rounds = int(early_stopping_rounds)
        self.eval_steps = int(eval_steps)
        self.weight_decay = float(weight_decay)
        self.seed = int(seed)
        self.num_threads = int(num_threads)
        if self.lookback_window < 2:
            raise ValueError("lookback_window必须至少为2")
        torch.manual_seed(self.seed)
        self.device = _torch_device(torch)
        if self.device.type == "cuda":
            torch.cuda.manual_seed_all(self.seed)
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
        train, valid = dataset.prepare(
            ["train", "valid"], col_set=["feature", "label"],
            data_key=DataHandlerLP.DK_L,
        )
        if not len(train) or not len(valid):
            raise ValueError(f"{self.model_name}训练集或验证集为空")
        optimizer = torch.optim.AdamW(
            [*self.encoder.parameters(), *self.head.parameters()],
            lr=self.learning_rate,
            weight_decay=self.weight_decay,
        )
        loss_fn = torch.nn.MSELoss()
        generator = torch.Generator().manual_seed(self.seed)
        best_loss = float("inf")
        best_encoder = deepcopy(self.encoder.state_dict())
        best_head = deepcopy(self.head.state_dict())
        stale = 0
        successful_steps = 0
        evaluations.update({"train": [], "valid": [], "steps": []})
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
            train_loss = loss_fn(self._forward(x_train), y_train)
            train_loss.backward()
            optimizer.step()
            if step % self.eval_steps != 0 and step != self.max_steps:
                continue
            valid_loss = self._validation_loss(valid, loss_fn, cancellation)
            evaluations["train"].append(float(train_loss.item()))
            evaluations["valid"].append(float(valid_loss))
            evaluations["steps"].append(step)
            if progress is not None:
                progress(
                    "training", min(80, 58 + int(22 * step / self.max_steps)),
                    {"iteration": step, "total_iterations": self.max_steps},
                )
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
        if successful_steps and not np.isfinite(best_loss):
            valid_loss = self._validation_loss(valid, loss_fn, cancellation)
            evaluations["train"].append(float(train_loss.item()))
            evaluations["valid"].append(float(valid_loss))
            evaluations["steps"].append(self.max_steps)
            best_loss = valid_loss
            best_encoder = deepcopy(self.encoder.state_dict())
            best_head = deepcopy(self.head.state_dict())
        if successful_steps == 0 or not np.isfinite(best_loss):
            raise ValueError(f"{self.model_name}没有可用的完整历史窗口样本")
        self.encoder.load_state_dict(best_encoder)
        self.head.load_state_dict(best_head)
        self.encoder.eval()
        self.head.eval()
        self.fitted = True
        try:
            from qlib.workflow import R

            for step, (train_loss, valid_loss) in enumerate(
                zip(evaluations["train"], evaluations["valid"]), start=1,
            ):
                R.log_metrics(train_loss=train_loss, valid_loss=valid_loss, step=step)
        except (AttributeError, RuntimeError):
            pass

    def _validation_loss(self, sampler: Any, loss_fn: Any, cancellation: Any) -> float:
        import torch

        total_loss = 0.0
        total_rows = 0
        self.encoder.eval()
        self.head.eval()
        with torch.no_grad():
            for start in range(0, len(sampler), self.batch_size):
                if cancellation is not None:
                    cancellation.checkpoint()
                indices = list(range(start, min(len(sampler), start + self.batch_size)))
                features, labels, _ = _sequence_batch(
                    sampler, indices, self.input_dim, with_label=True,
                )
                if not len(features):
                    continue
                values = self._forward(torch.as_tensor(features, dtype=torch.float32, device=self.device))
                target = torch.as_tensor(labels, dtype=torch.float32, device=self.device)
                loss = float(loss_fn(values, target).item())
                total_loss += loss * len(features)
                total_rows += len(features)
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

    def predict_sampler(self, sampler: Any) -> tuple[np.ndarray, list[int]]:
        import torch

        if not self.fitted:
            raise ValueError(f"{self.model_name}模型尚未训练")
        predictions: list[np.ndarray] = []
        positions: list[int] = []
        self.encoder.eval()
        self.head.eval()
        with torch.no_grad():
            for start in range(0, len(sampler), self.batch_size):
                indices = list(range(start, min(len(sampler), start + self.batch_size)))
                features, _, valid_positions = _sequence_batch(
                    sampler, indices, self.input_dim, with_label=False,
                )
                if not len(features):
                    continue
                output = self._forward(
                    torch.as_tensor(features, dtype=torch.float32, device=self.device),
                ).cpu().numpy().astype(float)
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
        batch_size: int = 256, early_stopping_rounds: int = 10,
        eval_steps: int = 10, weight_decay: float = 0.0001,
        seed: int = 42, num_threads: int = 4,
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
    "QlibTorchLSTMModel", "QlibTorchMLPModel", "QlibTorchTransformerLSTMModel",
]
