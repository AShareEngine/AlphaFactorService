from __future__ import annotations

import argparse
import json
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from factor_service.research.size_selection_factors import (
    size_selection_factor_payloads,
)


def _request(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    body = None if payload is None else json.dumps(
        payload, ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        method=method,
        headers={
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        raw = exc.read()
        response = json.loads(raw) if raw else {}
        return exc.code, response


def register(base_url: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for payload in size_selection_factor_payloads():
        factor_id = str(payload["factor_id"])
        status, validation = _request(
            base_url,
            "POST",
            "/factor-formulas/validate",
            {
                "expression": payload["expression"],
                "params": payload["params"],
            },
        )
        if status != 200 or validation.get("valid") is not True:
            raise RuntimeError(
                f"{factor_id}公式校验失败: "
                f"{validation.get('error_message') or validation}"
            )
        desired = {
            **payload,
            "required_fields": list(validation.get("required_fields") or []),
        }
        existing_status, existing = _request(
            base_url,
            "GET",
            f"/factors/{quote(factor_id, safe='')}",
        )
        if existing_status == 200:
            comparable = {
                key: existing.get(key)
                for key in desired
            }
            if comparable != desired:
                raise RuntimeError(f"{factor_id}已存在但定义不同，拒绝覆盖")
            results.append({
                "factor_id": factor_id,
                "action": "unchanged",
                "version": int(existing.get("version") or 0),
            })
            continue
        if existing_status != 404:
            raise RuntimeError(f"读取{factor_id}失败: {existing}")
        create_status, created = _request(
            base_url, "POST", "/factors", payload,
        )
        if create_status != 200:
            raise RuntimeError(f"创建{factor_id}失败: {created}")
        results.append({
            "factor_id": factor_id,
            "action": "created",
            "version": int(created.get("version") or 0),
        })
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url", default="http://127.0.0.1:8100",
    )
    args = parser.parse_args()
    print(json.dumps(register(args.base_url), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
