from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any, Mapping, Sequence

from psycopg.types.json import Jsonb

from factor_service.control_database import ControlDatabase, get_control_database


LEGACY_TRAINING_DATA_BINDING_SCHEMA_VERSION = (
    "alphablocks.model-training-data-bindings.v1"
)
PREVIOUS_TRAINING_DATA_BINDING_SCHEMA_VERSION = (
    "alphablocks.model-training-data-bindings.v2"
)
PREVIOUS_TRAINING_DATA_BINDING_SCHEMA_VERSION_V3 = (
    "alphablocks.model-training-data-bindings.v3"
)
TRAINING_DATA_BINDING_SCHEMA_VERSION = (
    "alphablocks.model-training-data-bindings.v4"
)
CONFIGURED_STOCK_POOL_SOURCE_SCHEMA_VERSION = (
    "alphablocks.configured-stock-pool-source.v1"
)
FROZEN_TRAINING_DATA_BINDING_SCHEMA_VERSION = (
    "alphablocks.frozen-model-training-data-bindings.v1"
)
TRAINING_RESOURCE_SETTING_KEY = "default"

STOCK_DAILY_BINDING_ID = "stock_daily_training"
SECURITY_MASTER_BINDING_ID = "security_master"
INDEX_MEMBERSHIP_BINDING_ID = "index_membership"
TRADING_CALENDAR_BINDING_ID = "trading_calendar"
STOCK_STATUS_BINDING_ID = "stock_status"
INDUSTRY_FEATURE_BINDING_ID = "stock_industry_one_hot"

SOURCE_TYPES = {"node"}
LEGACY_FROZEN_SOURCE_TYPES = {"node", "entity_asset"}
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_STOCK_POOLS = 100


def _role(
    role_id: str,
    label: str,
    *,
    required: bool,
    hints: Sequence[str],
) -> dict[str, Any]:
    return {
        "id": role_id,
        "label": label,
        "required": required,
        "hints": list(hints),
    }


BINDING_DEFINITIONS: dict[str, dict[str, Any]] = {
    STOCK_DAILY_BINDING_ID: {
        "label": "训练基础行情",
        "subtitle": "标签收益与每日价格",
        "description": "生成T+N标签；优先直接绑定后复权收盘价。",
        "required_for": "所有训练目标",
        "roles": [
            _role("trade_date", "交易日期", required=True,
                  hints=("trade_time", "trade_date", "date")),
            _role("instrument", "股票代码", required=True,
                  hints=("code", "market_code", "instrument")),
            _role("adjusted_close", "后复权收盘价", required=True,
                  hints=("close_adj", "adjusted_close")),
            _role("float_market_cap", "流通市值", required=False,
                  hints=("float_market_cap",)),
        ],
        "parameters": [],
    },
    SECURITY_MASTER_BINDING_ID: {
        "label": "证券历史主数据",
        "subtitle": "上市日期与全A边界",
        "description": (
            "用于识别A股、构建全A股票池，并计算上市交易日数量。"
        ),
        "required_for": "全A股票池或新股过滤",
        "roles": [
            _role("instrument", "证券代码", required=True,
                  hints=("code", "market_code", "instrument")),
            _role("security_type", "证券类型", required=True,
                  hints=("security_type", "type")),
            _role("listing_date", "上市日期", required=True,
                  hints=("start_date", "ipo_date", "listing_date")),
            _role("delisting_date", "退市日期", required=False,
                  hints=("end_date", "delisting_date")),
            _role("exchange", "交易所", required=False,
                  hints=("exchange",)),
        ],
        "parameters": [
            {
                "id": "stock_type_pattern",
                "label": "A股类型匹配值",
                "kind": "text",
                "default": "STOCK",
                "description": "证券类型包含该文本时视为A股，不区分大小写。",
            },
        ],
    },
    INDEX_MEMBERSHIP_BINDING_ID: {
        "label": "指数成分股票池",
        "subtitle": "历史纳入与剔除区间",
        "description": (
            "按交易日还原沪深300、中证500、中证800和中证1000历史成分。"
        ),
        "required_for": "非全A股票池",
        "roles": [
            _role("index_code", "指数代码", required=True,
                  hints=("index_code",)),
            _role("instrument", "成分股票代码", required=True,
                  hints=("con_code", "code", "instrument")),
            _role("in_date", "纳入日期", required=True,
                  hints=("in_date", "start_date")),
            _role("out_date", "剔除日期", required=False,
                  hints=("out_date", "end_date")),
        ],
        "parameters": [],
    },
    TRADING_CALENDAR_BINDING_ID: {
        "label": "交易日历",
        "subtitle": "切分、窗口与上市天数",
        "description": (
            "用于时间切分、T+N标签、序列模型回看窗口及上市交易日计算。"
        ),
        "required_for": "所有训练目标",
        "roles": [
            _role("trade_date", "交易日期", required=True,
                  hints=("trade_date", "date", "trade_time")),
        ],
        "parameters": [],
    },
    STOCK_STATUS_BINDING_ID: {
        "label": "股票历史状态",
        "subtitle": "ST与退市整理过滤",
        "description": (
            "按交易日执行非ST、非退市整理样本过滤，避免使用当前状态回填历史。"
        ),
        "required_for": "启用ST或退市整理过滤时",
        "roles": [
            _role("trade_date", "交易日期", required=True,
                  hints=("trade_date", "date", "trade_time")),
            _role("instrument", "股票代码", required=True,
                  hints=("market_code", "code", "instrument")),
            _role("is_st", "ST状态", required=True,
                  hints=("is_st", "is_st_sec")),
            _role("is_delisting", "退市整理状态", required=True,
                  hints=("is_withdrawal", "is_wd_sec", "is_delisting")),
            _role("is_suspended", "停牌状态", required=False,
                  hints=("is_suspended", "is_susp_sec")),
        ],
        "parameters": [],
    },
    INDUSTRY_FEATURE_BINDING_ID: {
        "label": "行业归属",
        "subtitle": "One-hot与行业轮动",
        "description": (
            "为个股行业One-hot和申万一级行业轮动提供逐日行业归属与权重。"
        ),
        "required_for": "行业One-hot或行业轮动",
        "roles": [
            _role("trade_date", "交易日期", required=True,
                  hints=("trade_date", "date")),
            _role("instrument", "股票代码", required=True,
                  hints=("con_code", "code", "market_code", "instrument")),
            _role("industry_code", "行业代码", required=True,
                  hints=("index_code", "industry_code")),
            _role("industry_name", "行业名称", required=False,
                  hints=("level1_name", "industry_level1_name", "industry_name")),
            _role("industry_level", "行业层级", required=False,
                  hints=("level_type", "industry_level")),
            _role("weight", "成分权重", required=False,
                  hints=("weight", "industry_weight")),
        ],
        "parameters": [
            {
                "id": "industry_level_value",
                "label": "行业层级值",
                "kind": "text",
                "default": "1",
                "description": "只保留这个层级；申万一级通常为1。",
            },
        ],
    },
}


class TrainingResourceRevisionConflict(ValueError):
    pass


def training_data_binding_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": binding_id,
            **json.loads(json.dumps(definition, ensure_ascii=False)),
        }
        for binding_id, definition in BINDING_DEFINITIONS.items()
    ]


def normalize_training_resource_settings(
    source: Mapping[str, Any] | None,
) -> dict[str, Any]:
    raw = _configuration_payload(source)
    unknown = sorted(set(raw) - {"schema_version", "bindings", "stock_pools"})
    if unknown:
        raise ValueError(
            "模型训练数据绑定包含未支持字段: " + ", ".join(unknown)
        )
    schema_version = str(
        raw.get("schema_version") or TRAINING_DATA_BINDING_SCHEMA_VERSION
    ).strip()
    if schema_version not in {
        LEGACY_TRAINING_DATA_BINDING_SCHEMA_VERSION,
        PREVIOUS_TRAINING_DATA_BINDING_SCHEMA_VERSION,
        PREVIOUS_TRAINING_DATA_BINDING_SCHEMA_VERSION_V3,
        TRAINING_DATA_BINDING_SCHEMA_VERSION,
    }:
        raise ValueError("模型训练数据绑定schema_version不受支持")
    bindings_source = raw.get("bindings") or {}
    if not isinstance(bindings_source, Mapping):
        raise ValueError("模型训练数据绑定bindings必须是对象")
    bindings_source = _without_retired_bindings(bindings_source)
    unknown_bindings = sorted(set(bindings_source) - set(BINDING_DEFINITIONS))
    if unknown_bindings:
        raise ValueError(
            "模型训练包含未支持的数据绑定: " + ", ".join(unknown_bindings)
        )
    return {
        "schema_version": TRAINING_DATA_BINDING_SCHEMA_VERSION,
        "bindings": {
            binding_id: _normalize_binding(
                binding_id,
                _upgrade_legacy_binding(
                    bindings_source.get(binding_id),
                    schema_version=schema_version,
                ),
            )
            for binding_id in BINDING_DEFINITIONS
        },
        "stock_pools": _normalize_stock_pools(raw.get("stock_pools")),
    }


def configured_stock_pool_sources(
    settings: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    normalized = normalize_training_resource_settings(settings)
    revision = int((settings or {}).get("revision") or 0)
    membership = normalized["bindings"][INDEX_MEMBERSHIP_BINDING_ID]
    ready = training_data_binding_ready(
        membership, INDEX_MEMBERSHIP_BINDING_ID,
    )
    return [
        {
            "schema_version": CONFIGURED_STOCK_POOL_SOURCE_SCHEMA_VERSION,
            "source_id": pool["id"],
            "source_kind": "configured_stock_pool",
            "label": pool["label"],
            "version": revision,
            "available": bool(pool["enabled"] and ready and revision > 0),
            "pit": True,
            "settings_revision": revision,
            "binding_id": INDEX_MEMBERSHIP_BINDING_ID,
            "binding_fingerprint": str(membership.get("fingerprint") or ""),
            "selector": {
                "field_role": "index_code",
                "operator": "eq",
                "value": pool["selector_value"],
            },
            "benchmark_code": pool["benchmark_code"],
            "config_fingerprint": pool["fingerprint"],
        }
        for pool in normalized["stock_pools"]
        if pool["enabled"]
    ]


def training_data_binding(
    settings: Mapping[str, Any] | None,
    binding_id: str = INDUSTRY_FEATURE_BINDING_ID,
) -> dict[str, Any]:
    normalized = normalize_training_resource_settings(settings)
    binding = normalized["bindings"].get(str(binding_id or ""))
    if not isinstance(binding, dict):
        raise ValueError(f"模型训练数据绑定不存在: {binding_id}")
    return dict(binding)


def training_data_binding_ready(
    binding: Mapping[str, Any] | None,
    binding_id: str = INDUSTRY_FEATURE_BINDING_ID,
    *,
    allow_legacy_source: bool = False,
) -> bool:
    definition = BINDING_DEFINITIONS.get(str(binding_id or ""))
    if not definition or not isinstance(binding, Mapping):
        return False
    if binding.get("enabled") is not True:
        return False
    source_type = str(binding.get("source_type") or "")
    source_types = (
        LEGACY_FROZEN_SOURCE_TYPES if allow_legacy_source else SOURCE_TYPES
    )
    if source_type not in source_types:
        return False
    source_id = str(binding.get("source_id") or "").strip()
    provider_node_id = str(binding.get("provider_node_id") or "").strip()
    if not source_id:
        return False
    if not provider_node_id:
        return False
    if not allow_legacy_source and source_id != provider_node_id:
        return False
    fields = binding.get("field_bindings")
    required = {
        str(role["id"])
        for role in definition["roles"]
        if role.get("required") is True
    }
    return isinstance(fields, Mapping) and all(
        str(fields.get(role) or "").strip() for role in required
    )


def required_training_data_binding_ids(
    dataset_spec: Mapping[str, Any] | None,
) -> list[str]:
    spec = dict(dataset_spec or {})
    required = [STOCK_DAILY_BINDING_ID, TRADING_CALENDAR_BINDING_ID]
    universe_id = str(spec.get("universe_id") or "csi500")
    universe_source = spec.get("universe_source")
    registered_membership = (
        isinstance(universe_source, Mapping)
        and str(universe_source.get("source_kind") or "") == "entity_asset"
    )
    filters = spec.get("sample_filters")
    filters = dict(filters) if isinstance(filters, Mapping) else {}
    try:
        minimum_listing_days = int(
            filters.get("minimum_listing_trading_days", 60)
        )
    except (TypeError, ValueError):
        minimum_listing_days = 0
    if universe_id == "all_a" or minimum_listing_days > 0:
        required.append(SECURITY_MASTER_BINDING_ID)
    if universe_id != "all_a" and not registered_membership:
        required.append(INDEX_MEMBERSHIP_BINDING_ID)
    if (
        filters.get("exclude_st") is True
        or filters.get("exclude_delisting") is True
    ):
        required.append(STOCK_STATUS_BINDING_ID)
    target = str(spec.get("research_target") or "stock_selection")
    industry = spec.get("industry_feature")
    if target == "industry_rotation" or (
        isinstance(industry, Mapping) and industry.get("enabled") is True
    ):
        required.append(INDUSTRY_FEATURE_BINDING_ID)
    size_rotation = spec.get("size_rotation_feature")
    if isinstance(size_rotation, Mapping) and size_rotation.get("enabled") is True:
        required.append(INDEX_MEMBERSHIP_BINDING_ID)
    return list(dict.fromkeys(required))


def frozen_training_data_binding(
    settings: Mapping[str, Any],
    binding_id: str = INDUSTRY_FEATURE_BINDING_ID,
) -> dict[str, Any]:
    binding = training_data_binding(settings, binding_id)
    if not training_data_binding_ready(binding, binding_id):
        label = BINDING_DEFINITIONS[binding_id]["label"]
        raise ValueError(f"{label}尚未在设置中心绑定可用数据节点")
    return {
        **binding,
        "binding_id": binding_id,
        "settings_revision": int(settings.get("revision") or 0),
    }


def frozen_training_data_bindings(
    settings: Mapping[str, Any],
    binding_ids: Sequence[str],
) -> dict[str, Any]:
    revision = int(settings.get("revision") or 0)
    if revision < 1:
        raise ValueError("模型训练数据绑定配置版本无效")
    frozen: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for binding_id in dict.fromkeys(str(item) for item in binding_ids):
        binding = training_data_binding(settings, binding_id)
        if not training_data_binding_ready(binding, binding_id):
            missing.append(str(BINDING_DEFINITIONS[binding_id]["label"]))
            continue
        frozen[binding_id] = {
            **binding,
            "binding_id": binding_id,
            "settings_revision": revision,
        }
    if missing:
        raise ValueError("请先在设置中心完成数据绑定：" + "、".join(missing))
    return {
        "schema_version": FROZEN_TRAINING_DATA_BINDING_SCHEMA_VERSION,
        "settings_revision": revision,
        "bindings": frozen,
    }


def normalize_frozen_training_data_binding(
    source: Mapping[str, Any] | None,
) -> dict[str, Any]:
    raw = dict(source or {})
    binding_id = str(raw.pop("binding_id", "") or "").strip()
    if binding_id not in BINDING_DEFINITIONS:
        raise ValueError("模型训练数据绑定binding_id无效")
    revision = raw.pop("settings_revision", None)
    if type(revision) is not int or revision < 1:
        raise ValueError("模型训练数据绑定settings_revision无效")
    binding = _normalize_binding(
        binding_id, raw, allow_legacy_source=True,
    )
    if not training_data_binding_ready(
        binding, binding_id, allow_legacy_source=True,
    ):
        raise ValueError(
            f"{BINDING_DEFINITIONS[binding_id]['label']}数据绑定尚未配置完整"
        )
    return {
        **binding,
        "binding_id": binding_id,
        "settings_revision": revision,
    }


def normalize_frozen_training_data_bindings(
    source: Mapping[str, Any] | None,
    *,
    allow_empty: bool = False,
) -> dict[str, Any]:
    raw = dict(source or {})
    if not raw and allow_empty:
        return {
            "schema_version": FROZEN_TRAINING_DATA_BINDING_SCHEMA_VERSION,
            "settings_revision": 0,
            "bindings": {},
        }
    if (
        allow_empty
        and raw.get("settings_revision") == 0
        and raw.get("bindings") == {}
        and str(raw.get("schema_version") or "")
        == FROZEN_TRAINING_DATA_BINDING_SCHEMA_VERSION
    ):
        return {
            "schema_version": FROZEN_TRAINING_DATA_BINDING_SCHEMA_VERSION,
            "settings_revision": 0,
            "bindings": {},
        }
    unknown = sorted(
        set(raw) - {"schema_version", "settings_revision", "bindings"}
    )
    if unknown:
        raise ValueError(
            "冻结模型训练数据绑定包含未支持字段: " + ", ".join(unknown)
        )
    if str(raw.get("schema_version") or "") != (
        FROZEN_TRAINING_DATA_BINDING_SCHEMA_VERSION
    ):
        raise ValueError("冻结模型训练数据绑定schema_version不受支持")
    revision = raw.get("settings_revision")
    if type(revision) is not int or revision < 1:
        raise ValueError("冻结模型训练数据绑定settings_revision无效")
    bindings_source = raw.get("bindings")
    if not isinstance(bindings_source, Mapping):
        raise ValueError("冻结模型训练数据绑定bindings必须是对象")
    bindings_source = _without_retired_bindings(bindings_source)
    unknown_bindings = sorted(set(bindings_source) - set(BINDING_DEFINITIONS))
    if unknown_bindings:
        raise ValueError(
            "冻结模型训练包含未支持的数据绑定: "
            + ", ".join(unknown_bindings)
        )
    bindings: dict[str, dict[str, Any]] = {}
    for binding_id, source_binding in bindings_source.items():
        if not isinstance(source_binding, Mapping):
            raise ValueError(f"冻结数据绑定{binding_id}必须是对象")
        candidate = dict(source_binding)
        candidate.setdefault("binding_id", binding_id)
        candidate.setdefault("settings_revision", revision)
        normalized = normalize_frozen_training_data_binding(candidate)
        if normalized["settings_revision"] != revision:
            raise ValueError("冻结数据绑定配置版本不一致")
        bindings[binding_id] = normalized
    return {
        "schema_version": FROZEN_TRAINING_DATA_BINDING_SCHEMA_VERSION,
        "settings_revision": revision,
        "bindings": bindings,
    }


def frozen_data_binding(
    frozen_settings: Mapping[str, Any] | None,
    binding_id: str,
) -> dict[str, Any] | None:
    if not isinstance(frozen_settings, Mapping):
        return None
    normalized = normalize_frozen_training_data_bindings(
        frozen_settings, allow_empty=True,
    )
    binding = normalized["bindings"].get(binding_id)
    return dict(binding) if isinstance(binding, dict) else None


class TrainingResourceSettingsRepository:
    """PostgreSQL source of truth for model-training system data bindings."""

    def __init__(self, database: ControlDatabase | None = None) -> None:
        self.database = database or get_control_database()

    def get(self) -> dict[str, Any]:
        with self.database.connection() as conn:
            row = conn.execute(
                """
                SELECT revision, settings_json, updated_at
                FROM model_training_resource_settings
                WHERE setting_key = %s
                """,
                (TRAINING_RESOURCE_SETTING_KEY,),
            ).fetchone()
        if not row:
            raise RuntimeError("模型训练数据绑定尚未初始化，请执行控制库迁移")
        return _settings_view(row)

    def update(
        self, source: Mapping[str, Any], *, expected_revision: int,
    ) -> dict[str, Any]:
        settings = normalize_training_resource_settings(source)
        now = datetime.now(timezone.utc)
        with self.database.connection() as conn:
            with conn.transaction():
                current = conn.execute(
                    """
                    SELECT revision
                    FROM model_training_resource_settings
                    WHERE setting_key = %s
                    FOR UPDATE
                    """,
                    (TRAINING_RESOURCE_SETTING_KEY,),
                ).fetchone()
                if not current:
                    raise RuntimeError(
                        "模型训练数据绑定尚未初始化，请执行控制库迁移"
                    )
                revision = int(current["revision"])
                if revision != int(expected_revision):
                    raise TrainingResourceRevisionConflict(
                        "模型训练数据绑定已被其他页面更新，请刷新后重试"
                    )
                row = conn.execute(
                    """
                    UPDATE model_training_resource_settings
                    SET revision = revision + 1,
                        settings_json = %s,
                        updated_at = %s
                    WHERE setting_key = %s
                    RETURNING revision, settings_json, updated_at
                    """,
                    (Jsonb(settings), now, TRAINING_RESOURCE_SETTING_KEY),
                ).fetchone()
        return _settings_view(row)


def get_training_resource_settings() -> dict[str, Any]:
    return TrainingResourceSettingsRepository().get()


def _normalize_binding(
    binding_id: str,
    source: Any,
    *,
    allow_legacy_source: bool = False,
) -> dict[str, Any]:
    definition = BINDING_DEFINITIONS[binding_id]
    label = str(definition["label"])
    raw = dict(source or {}) if isinstance(source, Mapping) else {}
    parameter_ids = {
        str(item["id"]) for item in definition.get("parameters", ())
    }
    allowed = {
        "enabled",
        "source_type",
        "source_id",
        "source_label",
        "provider_node_id",
        "field_bindings",
        "catalog_updated_at",
        "fingerprint",
        *parameter_ids,
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(
            f"{label}数据绑定包含未支持字段: " + ", ".join(unknown)
        )
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"{label}数据绑定enabled必须是布尔值")
    source_type = str(raw.get("source_type") or "node").strip()
    source_types = (
        LEGACY_FROZEN_SOURCE_TYPES if allow_legacy_source else SOURCE_TYPES
    )
    if source_type not in source_types:
        raise ValueError(f"{label}数据来源只允许数据节点")
    source_id = _text(raw.get("source_id"), "source_id", 180, label)
    source_label = _text(raw.get("source_label"), "source_label", 180, label)
    provider_node_id = _text(
        raw.get("provider_node_id"), "provider_node_id", 180, label,
    )
    if (
        not allow_legacy_source
        and (source_id or provider_node_id)
        and source_id != provider_node_id
    ):
        raise ValueError(f"{label}数据来源必须与实际节点一致")
    role_ids = {str(role["id"]) for role in definition["roles"]}
    required_roles = {
        str(role["id"])
        for role in definition["roles"]
        if role.get("required") is True
    }
    field_source = raw.get("field_bindings") or {}
    if not isinstance(field_source, Mapping):
        raise ValueError(f"{label}field_bindings必须是对象")
    unknown_roles = sorted(set(field_source) - role_ids)
    if unknown_roles:
        raise ValueError(
            f"{label}包含未支持的字段角色: " + ", ".join(unknown_roles)
        )
    fields: dict[str, str] = {}
    for role in sorted(role_ids):
        field = str(field_source.get(role) or "").strip()
        if field and not IDENTIFIER.fullmatch(field):
            raise ValueError(f"{label}字段{role}不是合法节点字段名")
        fields[role] = field
    if enabled:
        if not source_id or not provider_node_id:
            raise ValueError(f"启用{label}前必须选择数据来源")
        missing = sorted(role for role in required_roles if not fields[role])
        if missing:
            raise ValueError(f"{label}缺少字段映射: " + ", ".join(missing))
    core: dict[str, Any] = {
        "enabled": enabled,
        "source_type": source_type,
        "source_id": source_id,
        "source_label": source_label,
        "provider_node_id": provider_node_id,
        "field_bindings": fields,
        "catalog_updated_at": _text(
            raw.get("catalog_updated_at"), "catalog_updated_at", 80, label,
        ),
    }
    for parameter in definition.get("parameters", ()):
        parameter_id = str(parameter["id"])
        value = raw.get(parameter_id, parameter.get("default"))
        if parameter.get("kind") == "number":
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{label}{parameter['label']}必须是数字") from exc
            if not 0 < number <= 1_000_000_000:
                raise ValueError(f"{label}{parameter['label']}必须大于0")
            core[parameter_id] = int(number) if number.is_integer() else number
        else:
            core[parameter_id] = _text(
                value, parameter_id, 80, label,
            )
    fingerprint = sha256(_canonical_json(core).encode("utf-8")).hexdigest()
    configured_fingerprint = str(raw.get("fingerprint") or "").strip()
    if configured_fingerprint and configured_fingerprint != fingerprint:
        raise ValueError(f"{label}数据绑定fingerprint与配置不一致")
    return {**core, "fingerprint": fingerprint}


def _configuration_payload(
    source: Mapping[str, Any] | None,
) -> dict[str, Any]:
    raw = dict(source or {})
    return {
        key: raw[key]
        for key in ("schema_version", "bindings", "stock_pools")
        if key in raw
    }


def _normalize_stock_pools(source: Any) -> list[dict[str, Any]]:
    if source is None:
        return []
    if not isinstance(source, list):
        raise ValueError("模型训练stock_pools必须是数组")
    if len(source) > MAX_STOCK_POOLS:
        raise ValueError(f"模型训练股票池最多{MAX_STOCK_POOLS}个")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(source):
        label = f"stock_pools[{index}]"
        if not isinstance(item, Mapping):
            raise ValueError(f"{label}必须是对象")
        raw = dict(item)
        unknown = sorted(set(raw) - {
            "id", "label", "enabled", "selector_value",
            "benchmark_code", "fingerprint",
        })
        if unknown:
            raise ValueError(
                f"{label}包含未支持字段: " + ", ".join(unknown)
            )
        pool_id = str(raw.get("id") or "").strip()
        if not IDENTIFIER.fullmatch(pool_id) or len(pool_id) > 80:
            raise ValueError(f"{label}.id不是合法稳定标识")
        if pool_id in seen:
            raise ValueError(f"股票池ID重复: {pool_id}")
        seen.add(pool_id)
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"{label}.enabled必须是布尔值")
        pool_label = str(raw.get("label") or pool_id).strip()
        if not pool_label or len(pool_label) > 80:
            raise ValueError(f"{label}.label不能为空或超过80字符")
        selector_value = str(raw.get("selector_value") or "").strip()
        if not selector_value or len(selector_value) > 80:
            raise ValueError(
                f"{label}.selector_value不能为空或超过80字符"
            )
        benchmark_code = str(
            raw.get("benchmark_code") or selector_value
        ).strip()
        if not benchmark_code or len(benchmark_code) > 80:
            raise ValueError(
                f"{label}.benchmark_code不能为空或超过80字符"
            )
        core = {
            "id": pool_id,
            "label": pool_label,
            "enabled": enabled,
            "selector_value": selector_value,
            "benchmark_code": benchmark_code,
        }
        fingerprint = sha256(
            _canonical_json(core).encode("utf-8")
        ).hexdigest()
        configured = str(raw.get("fingerprint") or "").strip().lower()
        if configured and configured != fingerprint:
            raise ValueError(f"{label}.fingerprint与配置不一致")
        result.append({**core, "fingerprint": fingerprint})
    return result


def _upgrade_legacy_binding(
    source: Any,
    *,
    schema_version: str,
) -> Any:
    if (
        schema_version == TRAINING_DATA_BINDING_SCHEMA_VERSION
        or not isinstance(source, Mapping)
    ):
        return source
    binding = dict(source)
    provider_node_id = str(binding.get("provider_node_id") or "").strip()
    if provider_node_id and (
        str(binding.get("source_type") or "") != "node"
        or str(binding.get("source_id") or "").strip() != provider_node_id
    ):
        binding["source_type"] = "node"
        binding["source_id"] = provider_node_id
        binding.pop("fingerprint", None)
    return binding


def _without_retired_bindings(
    source: Mapping[str, Any],
) -> dict[str, Any]:
    bindings = dict(source)
    bindings.pop("market_cap_pit", None)
    daily_source = bindings.get(STOCK_DAILY_BINDING_ID)
    if isinstance(daily_source, Mapping):
        daily = dict(daily_source)
        fields_source = daily.get("field_bindings")
        if isinstance(fields_source, Mapping):
            fields = dict(fields_source)
            changed = "close" in fields or "float_market_cap" not in fields
            fields.pop("close", None)
            if changed:
                daily["field_bindings"] = fields
                daily.pop("fingerprint", None)
        bindings[STOCK_DAILY_BINDING_ID] = daily
    return bindings


def _settings_view(row: Mapping[str, Any]) -> dict[str, Any]:
    settings = normalize_training_resource_settings(row.get("settings_json"))
    updated_at = row.get("updated_at")
    return {
        **settings,
        "revision": int(row.get("revision") or 0),
        "updated_at": (
            updated_at.isoformat() if hasattr(updated_at, "isoformat")
            else str(updated_at or "")
        ),
    }


def _text(
    value: Any, field: str, maximum: int, binding_label: str,
) -> str:
    text = str(value or "").strip()
    if len(text) > maximum:
        raise ValueError(f"{binding_label}数据绑定{field}过长")
    return text


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


__all__ = [
    "BINDING_DEFINITIONS",
    "CONFIGURED_STOCK_POOL_SOURCE_SCHEMA_VERSION",
    "FROZEN_TRAINING_DATA_BINDING_SCHEMA_VERSION",
    "INDEX_MEMBERSHIP_BINDING_ID",
    "INDUSTRY_FEATURE_BINDING_ID",
    "SECURITY_MASTER_BINDING_ID",
    "STOCK_DAILY_BINDING_ID",
    "STOCK_STATUS_BINDING_ID",
    "TRADING_CALENDAR_BINDING_ID",
    "TRAINING_DATA_BINDING_SCHEMA_VERSION",
    "TrainingResourceRevisionConflict",
    "TrainingResourceSettingsRepository",
    "configured_stock_pool_sources",
    "frozen_data_binding",
    "frozen_training_data_binding",
    "frozen_training_data_bindings",
    "get_training_resource_settings",
    "normalize_frozen_training_data_binding",
    "normalize_frozen_training_data_bindings",
    "normalize_training_resource_settings",
    "required_training_data_binding_ids",
    "training_data_binding",
    "training_data_binding_catalog",
    "training_data_binding_ready",
]
