"""批次、领取回执与冲正的纯规则。

批次是对账差异的一次交付。更正不重写历史：新清单以「新修订」叠加，
逐项与当前有效项比较，并只通过领取回执决定下游动作：

* 从未领取或领取未确认 → 旧回执作废，项内容可替换 / 撤销；
* 已确认 → 原回执（原凭据）保留，按回执生成且仅生成一次冲正；
* 旧回执在更正后才确认（乱序）→ 只挂在该回执自身，不进入新修订的进度。

批次编号与更正编号都由规范化内容摘要派生，因此相同请求重试得到同一编号。
"""
from __future__ import annotations

import json

from app.errors import DomainError
from app.manifest import canonical_digest


def _evidence_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_items(raw) -> list[dict]:
    """校验并规范化差异项；按 key 排序，保证摘要稳定。"""
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise DomainError("INVALID_ITEMS", "差异项必须是数组")
    items: list[dict] = []
    seen: set[str] = set()
    for index, row in enumerate(raw):
        if not isinstance(row, dict):
            raise DomainError("INVALID_ITEM", f"第 {index + 1} 项不是对象")
        key = row.get("key")
        status = row.get("status")
        if not isinstance(key, str) or not key.strip():
            raise DomainError("INVALID_ITEM", f"第 {index + 1} 项缺少有效 key")
        if not isinstance(status, str) or not status.strip():
            raise DomainError("INVALID_ITEM", f"第 {index + 1} 项缺少有效 status")
        if key in seen:
            raise DomainError("DUPLICATE_KEY", f"差异项 key 重复：{key}")
        seen.add(key)
        items.append({
            "key": key,
            "status": status,
            "evidence": _evidence_text(row.get("evidence")),
        })
    return sorted(items, key=lambda value: value["key"])


def batch_digest(items: list[dict]) -> str:
    return canonical_digest(items)


def batch_id_for(items: list[dict]) -> str:
    return "batch-" + batch_digest(items)[:12]


def correction_digest(batch_id: str, base_revision: int, items: list[dict]) -> str:
    return canonical_digest({"batch_id": batch_id, "base_revision": base_revision, "items": items})


def receipt_id_for(batch_id: str, item_key: str, generation: int, seq: int) -> str:
    return "rcpt-" + canonical_digest(
        {"batch_id": batch_id, "item_key": item_key, "generation": generation, "seq": seq}
    )[:12]


def reversal_id_for(receipt_id: str) -> str:
    return "rev-" + canonical_digest({"receipt_id": receipt_id})[:12]


def item_changed(old: dict, new: dict) -> bool:
    return (old["status"], old["evidence"]) != (new["status"], new["evidence"])


def diff_plan(current: list[dict], wanted: list[dict]) -> dict:
    """计算一次更正要撤销、替换、新增与保持不变的项。"""
    current_map = {row["key"]: row for row in current}
    wanted_map = {row["key"]: row for row in wanted}
    return {
        "withdrawn": sorted(current_map.keys() - wanted_map.keys()),
        "added": sorted(wanted_map.keys() - current_map.keys()),
        "replaced": sorted(
            key for key in current_map.keys() & wanted_map.keys()
            if item_changed(current_map[key], wanted_map[key])
        ),
        "unchanged": sorted(
            key for key in current_map.keys() & wanted_map.keys()
            if not item_changed(current_map[key], wanted_map[key])
        ),
    }
