from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


FACTOR_IDS = (
    "size_bucket",
    "momentum_10_adj",
    "reversal_5_adj",
    "realized_volatility_20",
    "amount_liquidity_20",
    "current_ratio_pit",
    "operating_cashflow_to_assets_pit",
    "operating_cashflow_to_profit_pit",
    "roe_quality_pit",
    "revenue_growth_pit",
    "profit_growth_pit",
    "eps_quality_pit",
)
ELIGIBILITY_ASSET_ID = "asset_stock_daily_stock_daily_real"
ELIGIBILITY_PROVIDER_NODE = "stock_daily_real"
ELIGIBILITY_FIELD = "is_wd_sec"
ELIGIBILITY_EXPRESSION = "stock_daily_real.is_wd_sec == false"


class FactorServiceError(RuntimeError):
    pass


def _request(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: int = 120,
) -> tuple[int, Any]:
    body = None if payload is None else json.dumps(
        payload, ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        method=method,
        headers={
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if body else {}),
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except HTTPError as exc:
        raw = exc.read()
        response = json.loads(raw) if raw else {}
        return exc.code, response


def _ok(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    expected: tuple[int, ...] = (200,),
) -> Any:
    status, result = _request(base_url, method, path, payload)
    if status not in expected:
        detail = result.get("detail") if isinstance(result, dict) else result
        raise FactorServiceError(f"{method} {path} failed ({status}): {detail}")
    return result


def _factor_contracts(base_url: str) -> list[dict[str, Any]]:
    definitions = _ok(
        base_url, "GET", "/factors?entity_type=stock&enabled=true",
    )
    by_id = {
        str(item.get("factor_id") or ""): item
        for item in definitions
        if isinstance(item, dict)
    }
    missing = [factor_id for factor_id in FACTOR_IDS if factor_id not in by_id]
    if missing:
        raise FactorServiceError("factor library is missing: " + ", ".join(missing))
    identity_request = {
        "items": [
            {
                "factor_id": factor_id,
                "factor_version": int(by_id[factor_id]["version"]),
                "entity_type": "stock",
                "params": {},
            }
            for factor_id in FACTOR_IDS
        ],
    }
    identities = _ok(
        base_url, "POST", "/factors/parameter-identities", identity_request,
    )
    identity_by_id = {
        str(item.get("factor_id") or ""): item
        for item in identities
        if isinstance(item, dict)
    }
    return [
        {
            "feature_kind": "factor",
            "factor_id": factor_id,
            "factor_version": int(by_id[factor_id]["version"]),
            "params_hash": str(identity_by_id[factor_id]["params_hash"]),
            "params": {},
            "label": str(by_id[factor_id].get("label") or factor_id),
            "category": str(by_id[factor_id].get("category") or "custom"),
        }
        for factor_id in FACTOR_IDS
    ]


def _configured_pool(
    response: dict[str, Any], source_id: str,
) -> dict[str, Any]:
    sources = response.get("universe_sources") or []
    for source in sources:
        if str(source.get("source_id") or "") == source_id:
            if source.get("available") is not True:
                raise FactorServiceError(f"configured pool is unavailable: {source_id}")
            return deepcopy(source)
    raise FactorServiceError(f"configured pool does not exist: {source_id}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _eligibility_field_filter(alphablocks_url: str) -> dict[str, Any]:
    asset_workspace = _ok(alphablocks_url, "GET", "/api/data-assets")
    assets = [
        item for item in asset_workspace.get("assets") or []
        if isinstance(item, dict)
        and str(item.get("capability") or item.get("asset_id") or "")
        == ELIGIBILITY_ASSET_ID
    ]
    if len(assets) != 1:
        raise FactorServiceError(
            f"expected one eligibility asset {ELIGIBILITY_ASSET_ID}, "
            f"found {len(assets)}"
        )
    asset = assets[0]
    fields = [
        item for item in asset.get("fields") or []
        if isinstance(item, dict)
        and str(item.get("name") or item.get("source") or "")
        == ELIGIBILITY_FIELD
    ]
    bindings = [
        item for item in asset.get("provider_bindings") or []
        if isinstance(item, dict)
        and str(item.get("provider_node") or "")
        == ELIGIBILITY_PROVIDER_NODE
    ]
    if len(fields) != 1 or len(bindings) != 1:
        raise FactorServiceError(
            "eligibility field cannot be resolved to one frozen provider binding"
        )
    field = fields[0]
    provider = bindings[0]
    sdk_asset = _ok(
        alphablocks_url, "GET", "/api/data-sdk/assets/stock",
    ).get("asset") or {}
    sdk_fields = [
        item for item in sdk_asset.get("fields") or []
        if isinstance(item, dict)
        and str(item.get("name") or "") == ELIGIBILITY_FIELD
        and ELIGIBILITY_PROVIDER_NODE in (item.get("source_nodes") or [])
        and ELIGIBILITY_ASSET_ID in (item.get("source_assets") or [])
    ]
    if len(sdk_fields) != 1:
        raise FactorServiceError(
            "Data SDK does not expose one typed is_wd_sec field for stock_daily_real"
        )
    sdk_field = sdk_fields[0]
    supported = {
        str(item or "").strip().lower()
        for item in sdk_field.get("supported_operators") or []
    }
    if "eq" not in supported:
        raise FactorServiceError("is_wd_sec does not support equality filtering")
    role_bindings = dict(provider.get("field_bindings") or {})
    source_field = str(field.get("source") or ELIGIBILITY_FIELD).strip()
    asset_updated_at = str(asset.get("updated_at") or "").strip()
    binding = {
        "source_type": "node",
        "source_id": ELIGIBILITY_PROVIDER_NODE,
        "source_label": str(asset.get("name") or ELIGIBILITY_ASSET_ID),
        "provider_node_id": ELIGIBILITY_PROVIDER_NODE,
        "provider_node_version": int(
            provider.get("provider_node_version") or 0
        ),
        "provider_node_version_id": str(
            provider.get("provider_node_version_id") or ""
        ),
        "provider_node_source_hash": str(
            provider.get("provider_node_source_hash") or ""
        ).lower(),
        "provider_node_updated_at": str(
            provider.get("provider_node_updated_at") or ""
        ),
        "field_bindings": {
            "trade_date": str(role_bindings.get("time") or ""),
            "instrument": str(role_bindings.get("entity") or ""),
            "value": source_field,
        },
        "catalog_updated_at": asset_updated_at,
    }
    required = (
        asset_updated_at,
        str(sdk_field.get("dtype") or ""),
        str(binding["provider_node_version_id"]),
        str(binding["provider_node_source_hash"]),
        str(binding["provider_node_updated_at"]),
        *binding["field_bindings"].values(),
    )
    if not all(required) or binding["provider_node_version"] < 1:
        raise FactorServiceError("eligibility field binding identity is incomplete")
    binding["fingerprint"] = sha256(
        _canonical_json(binding).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "alphablocks.universe-entity-field-filter.v1",
        "kind": "entity_field",
        "entity_id": "stock",
        "asset_id": ELIGIBILITY_ASSET_ID,
        "asset_updated_at": asset_updated_at,
        "provider_node": ELIGIBILITY_PROVIDER_NODE,
        "field": ELIGIBILITY_FIELD,
        "source_field": source_field,
        "data_type": str(sdk_field["dtype"]).strip().lower(),
        "operator": "eq",
        "value": False,
        "missing_policy": "exclude",
        "binding": binding,
    }


def build_training_payload(
    base_url: str,
    *,
    alphablocks_url: str,
    project_id: str,
    date_start: str,
    date_end: str,
    data_cutoff: str,
) -> dict[str, Any]:
    binding_response = _ok(
        base_url, "GET", "/model-research/training-data-bindings",
    )
    large_pool = _configured_pool(binding_response, "csi300")
    small_pool = _configured_pool(binding_response, "csi1000")
    return {
        "client_study_id": f"{project_id}-size-style-lgbm-v1",
        "title": "大小盘风格选股 Qlib LightGBM",
        "model_id": "size_style_stock_selection_lgbm",
        "dataset": {
            "pipeline_version": "alphablocks.dataset-pipeline.v9",
            "name": "全A大小盘风格选股因子数据集",
            "universe_id": "all_a",
            "sample_filters": {
                "minimum_listing_trading_days": 375,
            },
            "universe_field_filters": [
                _eligibility_field_filter(alphablocks_url)
            ],
            "preprocessing": {"enabled": True},
            "industry_feature": {"enabled": False},
            "size_rotation_feature": {
                "schema_version": "alphablocks.size-rotation-feature.v1",
                "enabled": True,
                "large_pool": large_pool,
                "small_pool": small_pool,
                "return_window": 10,
                "basket_size": 20,
                "regime_window": 60,
                "feature_names": [
                    "size_float_style",
                    "size_stock_momentum_interaction",
                    "size_rotation_regime_interaction",
                    "size_large_momentum_regime_interaction",
                ],
                "point_in_time": True,
            },
            "research_target": "stock_selection",
            "target_mode": "return",
            "label_horizon_trading_days": 5,
            "date_start": date_start,
            "date_end": date_end,
            "data_cutoff": data_cutoff,
            "split": {
                "mode": "ratio",
                "train": 0.6,
                "valid": 0.2,
                "test": 0.2,
                "embargo_days": 5,
            },
            "factors": _factor_contracts(base_url),
        },
        "model": {
            "kind": "lightgbm",
            "version": 1,
            "params": {
                "learning_rate": 0.02,
                "num_leaves": 31,
                "max_depth": -1,
                "n_estimators": 2000,
                "min_data_in_leaf": 300,
                "min_child_samples": 150,
                "path_smooth": 1.0,
                "bagging_freq": 5,
                "lambda_l1": 0.5,
                "lambda_l2": 1.0,
                "feature_fraction": 0.7,
                "bagging_fraction": 0.8,
                "early_stopping_rounds": 50,
                "num_threads": 8,
            },
        },
        "walk_forward": {"enabled": False},
        "execution": {
            "node_id": "autodl-pro-test-01",
            "max_runtime_minutes": 240,
        },
    }


def preflight(
    base_url: str, payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    result = _ok(
        base_url,
        "POST",
        "/model-research/dataset-preflight",
        {"dataset": payload["dataset"]},
    )
    preflight_result = dict(result["preflight"])
    frozen_payload = deepcopy(payload)
    frozen_payload["dataset"] = deepcopy(preflight_result["dataset"])
    return frozen_payload, preflight_result


def frozen_dataset_from_preview(
    base_url: str, preview_id: str,
) -> dict[str, Any]:
    response = _ok(
        base_url,
        "GET",
        f"/model-research/jobs/{quote(preview_id, safe='')}",
    )
    job = dict(response["job"])
    if str(job.get("kind") or "") != "dataset_preview":
        raise ValueError(f"{preview_id}不是Dataset Preview")
    dataset = dict((job.get("config_json") or {}).get("dataset") or {})
    if not dataset:
        raise ValueError(f"{preview_id}缺少冻结数据契约")
    return dataset


def run(
    base_url: str,
    *,
    alphablocks_url: str = "http://127.0.0.1:3000",
    action: str,
    project_id: str,
    date_start: str,
    date_end: str,
    data_cutoff: str,
    frozen_preview_id: str = "",
) -> dict[str, Any]:
    payload = build_training_payload(
        base_url,
        alphablocks_url=alphablocks_url,
        project_id=project_id,
        date_start=date_start,
        date_end=date_end,
        data_cutoff=data_cutoff,
    )
    if frozen_preview_id:
        payload["dataset"] = frozen_dataset_from_preview(
            base_url, frozen_preview_id,
        )
    frozen_payload, preflight_result = preflight(base_url, payload)
    result: dict[str, Any] = {
        "action": action,
        "project_id": project_id,
        "preflight": preflight_result,
        "factor_ids": list(FACTOR_IDS),
        "eligibility_expression": ELIGIBILITY_EXPRESSION,
        "frozen_preview_id": frozen_preview_id,
    }
    if action == "preflight":
        return result
    if action == "preview":
        preview = _ok(
            base_url,
            "POST",
            "/model-research/dataset-previews",
            frozen_payload,
            expected=(200, 202),
        )
        result["preview"] = preview["preview"]
        return result
    created = _ok(
        base_url,
        "POST",
        "/model-research/jobs",
        frozen_payload,
        expected=(200, 201),
    )
    job = dict(created["job"])
    dispatched = _ok(
        base_url,
        "POST",
        f"/model-research/jobs/{quote(str(job['job_id']), safe='')}/dispatch",
        {},
        expected=(200, 202),
    )
    result["job"] = job
    result["dispatch"] = dispatched
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument(
        "--alphablocks-url", default="http://127.0.0.1:3000",
    )
    parser.add_argument(
        "--action", choices=("preflight", "preview", "submit"),
        default="preflight",
    )
    parser.add_argument(
        "--project-id", default="proj_2b2d74f341f4439b",
    )
    parser.add_argument("--date-start", default="2020-01-02")
    parser.add_argument("--date-end", default="2026-06-30")
    parser.add_argument(
        "--data-cutoff", default="2026-08-28T15:00:00+08:00",
    )
    parser.add_argument("--frozen-preview-id", default="")
    args = parser.parse_args()
    datetime.fromisoformat(args.data_cutoff)
    print(json.dumps(
        run(
            args.base_url,
            alphablocks_url=args.alphablocks_url,
            action=args.action,
            project_id=args.project_id,
            date_start=args.date_start,
            date_end=args.date_end,
            data_cutoff=args.data_cutoff,
            frozen_preview_id=args.frozen_preview_id,
        ),
        ensure_ascii=False,
        indent=2,
        default=str,
    ))


if __name__ == "__main__":
    main()
