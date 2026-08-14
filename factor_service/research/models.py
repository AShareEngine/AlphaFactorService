from __future__ import annotations

from copy import deepcopy
from typing import Any

import numpy as np
import pandas as pd


class QlibTorchMLPModel:
    """Small deterministic MLP consuming Qlib DatasetH train/valid/test segments."""

    def __init__(
        self, *, input_dim: int, hidden_size: int = 64, layer_count: int = 2,
        learning_rate: float = 0.001, max_steps: int = 300, batch_size: int = 2048,
        early_stopping_rounds: int = 10, eval_steps: int = 10,
        weight_decay: float = 0.0001, seed: int = 42, num_threads: int = 4,
    ) -> None:
        import torch
        from torch import nn

        self.input_dim = int(input_dim)
        self.hidden_size = int(hidden_size)
        self.layer_count = int(layer_count)
        self.learning_rate = float(learning_rate)
        self.max_steps = int(max_steps)
        self.batch_size = int(batch_size)
        self.early_stopping_rounds = int(early_stopping_rounds)
        self.eval_steps = int(eval_steps)
        self.weight_decay = float(weight_decay)
        self.seed = int(seed)
        self.num_threads = int(num_threads)
        # The scheduler executes jobs in a background Python thread. Reconfiguring
        # PyTorch's global CPU pool from that thread can deadlock on Apple Silicon;
        # the service process owns the pool and its environment-level limits.
        torch.manual_seed(self.seed)
        layers: list[Any] = []
        width = self.input_dim
        for _ in range(self.layer_count):
            layers.extend((nn.Linear(width, self.hidden_size), nn.ReLU()))
            width = self.hidden_size
        layers.append(nn.Linear(width, 1))
        self.network = nn.Sequential(*layers)
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
        x_train = torch.as_tensor(train["feature"].values, dtype=torch.float32)
        y_train = torch.as_tensor(train["label"].values.reshape(-1), dtype=torch.float32)
        x_valid = torch.as_tensor(valid["feature"].values, dtype=torch.float32)
        y_valid = torch.as_tensor(valid["label"].values.reshape(-1), dtype=torch.float32)
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
        evaluations.update({"train": [], "valid": []})
        self.network.train()
        for step in range(1, self.max_steps + 1):
            if cancellation is not None:
                cancellation.checkpoint()
            indices = torch.randint(
                0, len(x_train), (min(self.batch_size, len(x_train)),), generator=generator,
            )
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
            values = torch.as_tensor(features.values, dtype=torch.float32)
            return self.network(values).reshape(-1).cpu().numpy().astype(float)

    def get_feature_importance(self) -> list[float]:
        first = next(layer for layer in self.network if layer.__class__.__name__ == "Linear")
        return first.weight.detach().abs().mean(dim=0).cpu().numpy().tolist()


__all__ = ["QlibTorchMLPModel"]
