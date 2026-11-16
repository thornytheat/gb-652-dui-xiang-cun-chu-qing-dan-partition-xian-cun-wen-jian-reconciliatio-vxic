"""M6 批次账本：创建、领取回执、确认处理、按回执更正与冲正。

设计要点（对应业务约束）：

1. 更正不重写已导出的差异文件，而是在原批次上追加「修订」。
2. 逐项按当前有效回执决定动作：
   * 从未领取 / 已领取未确认 → 回执作废，项可撤销或替换；
   * 已确认 → 保留原回执编号与内容，生成且只生成一次冲正。
3. 更正分两阶段：先把更正单以 pending 落库并提交，再在单个事务里完成
   撤销 / 替换 / 新增 / 冲正并标记 applied。中途失败只留下 pending，
   ``recover`` 可续做；冲正编号由回执编号派生，重试不会新增。
4. 领取、确认、更正共用 Store 的串行写锁；每一步都写 batch_events，
   撤销与领取并发时按事件序号可追溯。
5. 旧回执被更正作废后才确认（乱序）时，确认只落在该回执上并补一次
   冲正，新修订的项、进度与导出不受影响。
6. 导出按当前修订的有效回执逐行生成，行内容与领取回执一致。
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import io
import json

from app.errors import DomainError
from .batch import (
    batch_digest,
    batch_id_for,
    correction_digest,
    diff_plan,
    normalize_items,
    receipt_id_for,
    reversal_id_for,
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value):
    return json.loads(value)


def _event(db, batch_id, revision, event_type, *, item_key=None, receipt_id=None,
           reversal_id=None, detail=None, now=None):
    db.execute(
        "INSERT INTO batch_events (batch_id, revision, event_type, item_key, receipt_id,"
        " reversal_id, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (batch_id, revision, event_type, item_key, receipt_id, reversal_id,
         _json(detail or {}), now or utcnow()),
    )


# ---------------------------------------------------------------- 读取辅助

def _batch_row(db, batch_id):
    row = db.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    if row is None:
        raise DomainError("BATCH_NOT_FOUND", f"批次不存在：{batch_id}")
    return row


def _item_rows(db, batch_id, active=True):
    sql = "SELECT * FROM batch_items WHERE batch_id=?"
    if active:
        sql += " AND active=1"
    sql += " ORDER BY item_key, item_generation"
    return db.execute(sql, (batch_id,)).fetchall()


def _receipt_row(db, receipt_id):
    return db.execute("SELECT * FROM receipts WHERE receipt_id=?", (receipt_id,)).fetchone()


def _active_receipt(db, batch_id, item_key, generation):
    return db.execute(
        "SELECT * FROM receipts WHERE batch_id=? AND item_key=? AND item_generation=?"
        " AND active=1 ORDER BY receipt_seq DESC LIMIT 1",
        (batch_id, item_key, generation),
    ).fetchone()


def _item_view(row):
    return {
        "key": row["item_key"],
        "status": row["status"],
        "evidence": row["evidence"],
        "generation": row["item_generation"],
        "added_revision": row["revision_added"],
        "changed_revision": row["revision_changed"],
    }


def _receipt_view(row):
    return {
        "receipt_id": row["receipt_id"],
        "key": row["item_key"],
        "generation": row["item_generation"],
        "seq": row["receipt_seq"],
        "status": row["status"],
        "evidence": row["evidence"],
        "state": row["state"],
        "claimed_by": row["claimed_by"],
        "claimed_at": row["claimed_at"],
        "confirmed_by": row["confirmed_by"],
        "confirmed_at": row["confirmed_at"],
        "issued_revision": row["issued_revision"],
        "superseded_revision": row["superseded_revision"],
        "late_confirmed": bool(row["late_confirmed"]),
        "active": bool(row["active"]),
    }


# ---------------------------------------------------------------- 冲正

def _ensure_reversal(db, receipt, revision, reason, detail, now):
    """按回执确保唯一一条冲正；重复调用不新增。返回 (id, 是否新建)。"""
    reversal_id = reversal_id_for(receipt["receipt_id"])
    cursor = db.execute(
        "INSERT OR IGNORE INTO reversals (reversal_id, receipt_id, batch_id, item_key,"
        " revision, reason, state, created_at, applied_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 'applied', ?, ?)",
        (reversal_id, receipt["receipt_id"], receipt["batch_id"], receipt["item_key"],
         revision, reason, now, now),
    )
    created = bool(cursor.rowcount)
    if created:
        _event(db, receipt["batch_id"], revision, "reversal_created",
               item_key=receipt["item_key"], receipt_id=receipt["receipt_id"],
               reversal_id=reversal_id,
               detail={"reason": reason, **(detail or {})}, now=now)
    return reversal_id, created


def _reverse_if_detached(db, receipt, revision, reason, detail, now):
    """回执已确认、但其所属项代已不是当前有效项时，补一次冲正。"""
    if receipt["state"] != "confirmed":
        return None
    current = db.execute(
        "SELECT 1 FROM batch_items WHERE batch_id=? AND item_key=? AND item_generation=?"
        " AND active=1",
        (receipt["batch_id"], receipt["item_key"], receipt["item_generation"]),
    ).fetchone()
    if current is not None:
        return None
    reversal_id, _ = _ensure_reversal(db, receipt, revision, reason, detail, now)
    return reversal_id


# ---------------------------------------------------------------- 创建与查询

def _items_from_reconcile(result: dict) -> list[dict]:
    rows = []
    for status, keys in (
        ("MISSING", result.get("missing", [])),
        ("ORPHAN", result.get("orphan", [])),
        ("CONFLICT", result.get("conflicts", [])),
    ):
        for key in keys:
            rows.append({"key": key, "status": status, "evidence": "reconcile"})
    return rows


def create_batch(store, payload: dict) -> dict:
    raw_items = payload.get("items")
    if raw_items is None and isinstance(payload.get("reconcile"), dict):
        raw_items = _items_from_reconcile(payload["reconcile"])
    if raw_items is None and payload.get("from_reconcile"):
        from app.modules.m5_reconcile import core as reconcile_core
        raw_items = _items_from_reconcile(
            reconcile_core.run(payload.get("reconcile_payload") or {})
        )
    items = normalize_items(raw_items)
    if not items:
        raise DomainError("EMPTY_BATCH", "批次至少需要一个差异项")
    digest = batch_digest(items)
    batch_id = batch_id_for(items)
    now = utcnow()
    with store.transaction() as db:
        existing = db.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if existing is not None:
            return _detail(db, existing, replayed=True)
        db.execute(
            "INSERT INTO batches (batch_id, digest, revision, status, created_at, updated_at)"
            " VALUES (?, ?, 1, 'open', ?, ?)",
            (batch_id, digest, now, now),
        )
        for item in items:
            db.execute(
                "INSERT INTO batch_items (batch_id, item_key, item_generation, status, evidence,"
                " state, active, revision_added, revision_changed)"
                " VALUES (?, ?, 1, ?, ?, 'active', 1, 1, 1)",
                (batch_id, item["key"], item["status"], item["evidence"]),
            )
        _event(db, batch_id, 1, "batch_created",
               detail={"digest": digest, "item_count": len(items)}, now=now)
        return _detail(db, _batch_row(db, batch_id), replayed=False)


def list_batches(store) -> list[dict]:
    with store.read() as db:
        rows = db.execute("SELECT * FROM batches ORDER BY created_at, batch_id").fetchall()
        return [_summary(db, row) for row in rows]


def get_batch(store, batch_id: str) -> dict:
    recover(store, batch_id)
    with store.read() as db:
        return _detail(db, _batch_row(db, batch_id))


def _summary(db, batch):
    batch_id = batch["batch_id"]
    items = _item_rows(db, batch_id)
    # 当前修订的有效回执：只统计仍挂在有效项上的回执，
    # 已撤销/替换项的旧凭据单独保留在历史里。
    receipts = db.execute(
        "SELECT r.state FROM receipts r JOIN batch_items i"
        " ON i.batch_id=r.batch_id AND i.item_key=r.item_key"
        " AND i.item_generation=r.item_generation AND i.active=1"
        " WHERE r.batch_id=? AND r.active=1", (batch_id,)
    ).fetchall()
    reversals = db.execute(
        "SELECT COUNT(*) FROM reversals WHERE batch_id=?", (batch_id,)
    ).fetchone()[0]
    pending = db.execute(
        "SELECT COUNT(*) FROM batch_corrections WHERE batch_id=? AND state='pending'",
        (batch_id,),
    ).fetchone()[0]
    withdrawn = db.execute(
        "SELECT COUNT(*) FROM batch_items WHERE batch_id=? AND active=0 AND state='withdrawn'",
        (batch_id,),
    ).fetchone()[0]
    replaced = db.execute(
        "SELECT COUNT(*) FROM batch_items WHERE batch_id=? AND active=0 AND state='replaced'",
        (batch_id,),
    ).fetchone()[0]
    confirmed = sum(1 for row in receipts if row["state"] == "confirmed")
    claimed = sum(1 for row in receipts if row["state"] == "claimed")
    return {
        "batch_id": batch_id,
        "digest": batch["digest"],
        "revision": batch["revision"],
        "status": batch["status"],
        "created_at": batch["created_at"],
        "updated_at": batch["updated_at"],
        "item_count": len(items),
        "receipt_count": len(receipts),
        "claimed_count": claimed,
        "confirmed_count": confirmed,
        "unclaimed_count": len(items) - len(receipts),
        "reversal_count": reversals,
        "pending_correction_count": pending,
        "withdrawn_count": withdrawn,
        "replaced_count": replaced,
    }


def _detail(db, batch, replayed: bool = False) -> dict:
    batch_id = batch["batch_id"]
    active_items = _item_rows(db, batch_id)
    items = [dict(_item_view(row), receipt=None) for row in active_items]
    receipts = db.execute(
        "SELECT * FROM receipts WHERE batch_id=? ORDER BY item_key, item_generation, receipt_seq",
        (batch_id,),
    ).fetchall()
    active_item_ids = {(row["item_key"], row["item_generation"]) for row in active_items}
    current_receipts = [
        _receipt_view(row) for row in receipts
        if row["active"] and (row["item_key"], row["item_generation"]) in active_item_ids
    ]
    historical_receipts = [
        _receipt_view(row) for row in receipts
        if not row["active"] or (row["item_key"], row["item_generation"]) not in active_item_ids
    ]
    for item in items:
        item["receipt"] = next(
            (view for view in current_receipts
             if view["key"] == item["key"] and view["generation"] == item["generation"]),
            None,
        )
    reversals = db.execute(
        "SELECT * FROM reversals WHERE batch_id=? ORDER BY created_at, reversal_id", (batch_id,)
    ).fetchall()
    corrections = db.execute(
        "SELECT * FROM batch_corrections WHERE batch_id=? ORDER BY created_at, revision",
        (batch_id,),
    ).fetchall()
    events = db.execute(
        "SELECT * FROM batch_events WHERE batch_id=? ORDER BY seq", (batch_id,)
    ).fetchall()
    summary = _summary(db, batch)
    summary.update({
        "replayed": replayed,
        "items": items,
        "receipts": current_receipts,
        "receipt_history": historical_receipts,
        "reversals": [{
            "reversal_id": row["reversal_id"],
            "receipt_id": row["receipt_id"],
            "key": row["item_key"],
            "revision": row["revision"],
            "reason": row["reason"],
            "state": row["state"],
            "created_at": row["created_at"],
            "applied_at": row["applied_at"],
        } for row in reversals],
        "corrections": [{
            "digest": row["digest"],
            "revision": row["revision"],
            "reason": row["reason"],
            "state": row["state"],
            "created_at": row["created_at"],
        } for row in corrections],
        "events": [{
            "seq": row["seq"],
            "revision": row["revision"],
            "type": row["event_type"],
            "key": row["item_key"],
            "receipt_id": row["receipt_id"],
            "reversal_id": row["reversal_id"],
            "detail": _loads(row["detail"]),
            "created_at": row["created_at"],
        } for row in events],
        "progress": {
            "revision": batch["revision"],
            "claimed": summary["claimed_count"],
            "confirmed": summary["confirmed_count"],
            "unclaimed": summary["unclaimed_count"],
            "withdrawn": summary["withdrawn_count"],
            "replaced": summary["replaced_count"],
            "reversals": summary["reversal_count"],
            "pending_corrections": summary["pending_correction_count"],
        },
    })
    return summary


# ---------------------------------------------------------------- 领取

def claim(store, batch_id: str, payload: dict) -> dict:
    worker = payload.get("worker") or payload.get("claimed_by")
    if not isinstance(worker, str) or not worker.strip():
        raise DomainError("WORKER_REQUIRED", "领取必须提供 worker")
    worker = worker.strip()
    keys = payload.get("keys")
    recover(store, batch_id)
    now = utcnow()
    with store.transaction() as db:
        batch = _batch_row(db, batch_id)
        items = _item_rows(db, batch_id)
        if keys is not None:
            if not isinstance(keys, list):
                raise DomainError("INVALID_KEYS", "keys 必须是数组")
            wanted = set(keys)
            known = {row["item_key"] for row in items}
            unknown = sorted(wanted - known)
            if unknown:
                raise DomainError("UNKNOWN_ITEM", f"批次中不存在差异项：{', '.join(unknown)}")
            items = [row for row in items if row["item_key"] in wanted]
        issued, replayed, contended = [], [], []
        for item in items:
            receipt = _active_receipt(db, batch_id, item["item_key"], item["item_generation"])
            if receipt is None:
                seq = db.execute(
                    "SELECT COALESCE(MAX(receipt_seq), 0) + 1 FROM receipts"
                    " WHERE batch_id=? AND item_key=? AND item_generation=?",
                    (batch_id, item["item_key"], item["item_generation"]),
                ).fetchone()[0]
                receipt_id = receipt_id_for(batch_id, item["item_key"],
                                            item["item_generation"], seq)
                db.execute(
                    "INSERT INTO receipts (receipt_id, batch_id, item_key, item_generation,"
                    " receipt_seq, status, evidence, state, active, issued_revision,"
                    " claimed_by, claimed_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 'claimed', 1, ?, ?, ?)",
                    (receipt_id, batch_id, item["item_key"], item["item_generation"], seq,
                     item["status"], item["evidence"], batch["revision"], worker, now),
                )
                _event(db, batch_id, batch["revision"], "claimed",
                       item_key=item["item_key"], receipt_id=receipt_id,
                       detail={"worker": worker}, now=now)
                issued.append(_receipt_view(_receipt_row(db, receipt_id)))
            elif receipt["state"] == "claimed" and receipt["claimed_by"] != worker:
                contended.append(_receipt_view(receipt))
                _event(db, batch_id, batch["revision"], "claim_contended",
                       item_key=item["item_key"], receipt_id=receipt["receipt_id"],
                       detail={"worker": worker, "holder": receipt["claimed_by"]}, now=now)
            else:
                replayed.append(_receipt_view(receipt))
        db.execute("UPDATE batches SET updated_at=? WHERE batch_id=?", (now, batch_id))
        current = _item_rows(db, batch_id)
        ids = {(row["item_key"], row["item_generation"]) for row in current}
        return {
            "batch_id": batch_id,
            "revision": batch["revision"],
            "issued": issued,
            "replayed": replayed,
            "contended": contended,
            "receipts": [
                _receipt_view(row) for row in db.execute(
                    "SELECT * FROM receipts WHERE batch_id=? AND active=1 ORDER BY item_key",
                    (batch_id,)).fetchall()
                if (row["item_key"], row["item_generation"]) in ids
            ],
            "progress": _summary(db, _batch_row(db, batch_id)),
        }


# ---------------------------------------------------------------- 确认

def _select_receipts(db, batch_id, payload):
    receipt_ids = payload.get("receipt_ids")
    keys = payload.get("keys")
    if receipt_ids is not None:
        if not isinstance(receipt_ids, list):
            raise DomainError("INVALID_RECEIPTS", "receipt_ids 必须是数组")
        rows = []
        for receipt_id in receipt_ids:
            row = _receipt_row(db, receipt_id)
            if row is None or row["batch_id"] != batch_id:
                raise DomainError("RECEIPT_NOT_FOUND", f"回执不存在：{receipt_id}")
            rows.append(row)
        return rows
    if keys is not None:
        if not isinstance(keys, list):
            raise DomainError("INVALID_KEYS", "keys 必须是数组")
        rows = []
        for key in keys:
            row = db.execute(
                "SELECT * FROM receipts WHERE batch_id=? AND item_key=? AND active=1"
                " ORDER BY item_generation DESC, receipt_seq DESC LIMIT 1",
                (batch_id, key),
            ).fetchone()
            if row is None:
                raise DomainError("RECEIPT_NOT_FOUND", f"差异项尚无有效回执，不能确认：{key}")
            rows.append(row)
        return rows
    # 默认只确认「当前修订有效项」上已领取未确认的回执
    return db.execute(
        "SELECT r.* FROM receipts r JOIN batch_items i"
        " ON i.batch_id=r.batch_id AND i.item_key=r.item_key"
        " AND i.item_generation=r.item_generation AND i.active=1"
        " WHERE r.batch_id=? AND r.active=1 AND r.state='claimed'"
        " ORDER BY r.item_key", (batch_id,)
    ).fetchall()


def confirm(store, batch_id: str, payload: dict) -> dict:
    worker = payload.get("worker") or payload.get("confirmed_by")
    if not isinstance(worker, str) or not worker.strip():
        raise DomainError("WORKER_REQUIRED", "确认必须提供 worker")
    worker = worker.strip()
    recover(store, batch_id)
    now = utcnow()
    with store.transaction() as db:
        batch = _batch_row(db, batch_id)
        rows = _select_receipts(db, batch_id, payload)
        confirmed, replayed, late = [], [], []
        for receipt in rows:
            if receipt["state"] == "confirmed":
                replayed.append(_receipt_view(receipt))
                continue
            if receipt["state"] == "superseded":
                db.execute(
                    "UPDATE receipts SET state='confirmed', confirmed_by=?, confirmed_at=?,"
                    " late_confirmed=1 WHERE receipt_id=?",
                    (worker, now, receipt["receipt_id"]),
                )
                fresh = _receipt_row(db, receipt["receipt_id"])
                reversal_id, _ = _ensure_reversal(
                    db, fresh, batch["revision"], "LATE_CONFIRMATION",
                    {"worker": worker, "late": True}, now,
                )
                _event(db, batch_id, batch["revision"], "late_confirmed",
                       item_key=receipt["item_key"], receipt_id=receipt["receipt_id"],
                       reversal_id=reversal_id,
                       detail={"worker": worker,
                               "note": "旧回执乱序确认，只作用于该回执，不改新修订进度"},
                       now=now)
                late.append(_receipt_view(_receipt_row(db, receipt["receipt_id"])))
                continue
            db.execute(
                "UPDATE receipts SET state='confirmed', confirmed_by=?, confirmed_at=?"
                " WHERE receipt_id=?",
                (worker, now, receipt["receipt_id"]),
            )
            fresh = _receipt_row(db, receipt["receipt_id"])
            reversal_id = _reverse_if_detached(
                db, fresh, batch["revision"], "REVOKED_BEFORE_CONFIRMATION",
                {"worker": worker}, now,
            )
            _event(db, batch_id, batch["revision"], "confirmed",
                   item_key=receipt["item_key"], receipt_id=receipt["receipt_id"],
                   reversal_id=reversal_id, detail={"worker": worker}, now=now)
            confirmed.append(_receipt_view(_receipt_row(db, receipt["receipt_id"])))
        db.execute("UPDATE batches SET updated_at=? WHERE batch_id=?", (now, batch_id))
        return {
            "batch_id": batch_id,
            "revision": batch["revision"],
            "confirmed": confirmed,
            "replayed": replayed,
            "late_confirmed": late,
            "progress": _summary(db, _batch_row(db, batch_id)),
        }


# ---------------------------------------------------------------- 更正

def _stale(store, batch_id: str, revision: int, detail: dict) -> None:
    """在独立事务里记录一次版本过期的拒绝事件（不随主事务回滚丢失）。"""
    with store.transaction() as db:
        _event(db, batch_id, revision, "correction_stale", detail=detail)


def correct(store, batch_id: str, payload: dict) -> dict:
    raw_items = payload.get("items")
    if raw_items is None and isinstance(payload.get("reconcile"), dict):
        raw_items = _items_from_reconcile(payload["reconcile"])
    wanted = normalize_items(raw_items)
    expected = payload.get("expected_revision")
    if expected is not None and not isinstance(expected, int):
        raise DomainError("INVALID_REVISION", "expected_revision 必须是整数")
    reason = payload.get("reason") or "清单更正"
    # 持有跨事务写锁：查重、pending 落库、应用、重新领取之间不会被插队
    with store.lock():
        recover(store, batch_id)
        now = utcnow()
        digest = None
        with store.read() as db:
            batch = _batch_row(db, batch_id)
            base_revision = batch["revision"]
            already_in_state = [
                {"key": row["key"], "status": row["status"], "evidence": row["evidence"]}
                for row in (_item_view(item) for item in _item_rows(db, batch_id))
            ]
            # 同一内容重复提交按已受理处理：
            #  * 已应用且基线正是本次期望版本（或当前已是目标状态）→ 重放，不重复冲正；
            #  * 仍在 pending（上次写到一半）→ 续做同一张更正单。
            for row in db.execute(
                "SELECT * FROM batch_corrections WHERE batch_id=?"
                " ORDER BY created_at, revision",
                (batch_id,),
            ).fetchall():
                body = _loads(row["payload"])
                if body.get("items") != wanted:
                    continue
                if row["state"] == "applied":
                    if (expected is None and already_in_state == wanted) or \
                            (expected is not None and body.get("base_revision") == expected):
                        return _detail(db, batch, replayed=True)
                    continue
                if row["state"] == "pending":
                    digest = row["digest"]
                    break
        if digest is None:
            if expected is not None and expected != base_revision:
                _stale(store, batch_id, base_revision,
                       {"expected_revision": expected,
                        "current_revision": base_revision})
                raise DomainError(
                    "STALE_VERSION",
                    f"提交版本已过期：期望 {expected}，当前 {base_revision}")
            digest = correction_digest(batch_id, base_revision, wanted)
            with store.transaction() as db:
                current = _batch_row(db, batch_id)
                if current["revision"] != base_revision:
                    _event(db, batch_id, current["revision"], "correction_stale",
                           detail={"expected_revision": base_revision,
                                   "current_revision": current["revision"]}, now=now)
                    raise DomainError(
                        "STALE_VERSION",
                        f"更正基于修订 {base_revision}，当前已是 {current['revision']}")
                existing = db.execute(
                    "SELECT state FROM batch_corrections WHERE batch_id=? AND digest=?",
                    (batch_id, digest),
                ).fetchone()
                if existing is not None and existing["state"] == "applied":
                    return _detail(db, current, replayed=True)
                if existing is None:
                    db.execute(
                        "INSERT INTO batch_corrections (batch_id, revision, digest, payload,"
                        " reason, state, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                        (batch_id, base_revision + 1, digest,
                         _json({"batch_id": batch_id, "base_revision": base_revision,
                                "items": wanted}), reason, now),
                    )
                    _event(db, batch_id, base_revision + 1, "correction_requested",
                           detail={"correction_id": "corr-" + digest[:12],
                                   "correction_digest": digest,
                                   "base_revision": base_revision,
                                   "target_revision": base_revision + 1,
                                   "reason": reason,
                                   "item_count": len(wanted)}, now=now)
        # 第二阶段：独立事务应用；失败只留 pending，可 recover 续做
        return _apply_pending(store, batch_id, digest)


class _Stale(Exception):
    """内部信号：更正基线已过期，需要在回滚后单独记录审计。"""


def _apply_pending(store, batch_id, digest, fail_after=None):
    """应用一条 pending 更正单；可从失败中恢复，冲正按回执去重。

    ``fail_after`` 仅用于测试注入：在完成若干步骤后抛出异常，模拟
    写到一半崩溃；已提交的更正单仍在 pending，可被 recover 续做。
    整个应用过程持有写锁，因此「校验基线 → 应用」之间不会被插队。
    """
    with store.lock():
        try:
            return _apply_locked(store, batch_id, digest, fail_after)
        except _Stale as signal:
            now = utcnow()
            with store.transaction() as db:
                db.execute(
                    "UPDATE batch_corrections SET state='stale'"
                    " WHERE batch_id=? AND digest=?", (batch_id, digest))
                _event(db, batch_id, signal.revision, "correction_stale",
                       detail={"stored_base": signal.base,
                               "current_revision": signal.revision}, now=now)
            raise DomainError(
                "STALE_VERSION",
                f"更正基于修订 {signal.base}，当前已是 {signal.revision}") from None


def _apply_locked(store, batch_id, digest, fail_after=None):
    now = utcnow()
    with store.transaction() as db:
        row = db.execute(
            "SELECT * FROM batch_corrections WHERE batch_id=? AND digest=?",
            (batch_id, digest),
        ).fetchone()
        if row is None:
            raise DomainError("CORRECTION_NOT_FOUND", "更正单不存在")
        if row["state"] == "applied":
            return _detail(db, _batch_row(db, batch_id), replayed=True)
        batch = _batch_row(db, batch_id)
        body = _loads(row["payload"])
        wanted = normalize_items(body.get("items"))
        base_revision = body.get("base_revision")
        if row["state"] == "stale" or base_revision != batch["revision"]:
            signal = _Stale()
            signal.base = base_revision
            signal.revision = batch["revision"]
            raise signal
        current = [_item_view(item) for item in _item_rows(db, batch_id)]
        plan = diff_plan(current, wanted)
        changed = bool(plan["withdrawn"] or plan["added"] or plan["replaced"])
        revision = batch["revision"] + 1 if changed else batch["revision"]
        current_map = {item["key"]: item for item in current}
        wanted_map = {row["key"]: row for row in wanted}
        done = 0

        for key in plan["withdrawn"] + plan["replaced"]:
            item = current_map[key]
            receipt = _active_receipt(db, batch_id, key, item["generation"])
            if receipt is not None:
                if receipt["state"] == "confirmed":
                    # 原凭据保留内容与编号，仅生成一次冲正；它不再是当前凭据
                    _ensure_reversal(
                        db, receipt, revision, "WITHDRAW_CONFIRMED_ITEM",
                        {"correction": digest}, now,
                    )
                    db.execute(
                        "UPDATE receipts SET active=0, superseded_revision=?,"
                        " superseded_at=? WHERE receipt_id=?",
                        (revision, now, receipt["receipt_id"]),
                    )
                else:
                    db.execute(
                        "UPDATE receipts SET active=0, state='superseded',"
                        " superseded_revision=?, superseded_at=? WHERE receipt_id=?",
                        (revision, now, receipt["receipt_id"]),
                    )
                    _event(db, batch_id, revision, "receipt_superseded",
                           item_key=key, receipt_id=receipt["receipt_id"],
                           detail={"correction": digest}, now=now)
            state = "withdrawn" if key in plan["withdrawn"] else "replaced"
            db.execute(
                "UPDATE batch_items SET active=0, state=?, superseded_revision=?"
                " WHERE batch_id=? AND item_key=? AND item_generation=? AND active=1",
                (state, revision, batch_id, key, item["generation"]),
            )
            _event(db, batch_id, revision,
                   "item_withdrawn" if state == "withdrawn" else "item_replaced",
                   item_key=key,
                   detail={"generation": item["generation"], "status": item["status"],
                           "correction": digest}, now=now)
            done += 1
            _maybe_fail(fail_after, done)

        for key in plan["added"] + plan["replaced"]:
            item = wanted_map[key]
            generation = db.execute(
                "SELECT COALESCE(MAX(item_generation), 0) + 1 FROM batch_items"
                " WHERE batch_id=? AND item_key=?", (batch_id, key),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO batch_items (batch_id, item_key, item_generation, status,"
                " evidence, state, active, revision_added, revision_changed)"
                " VALUES (?, ?, ?, ?, ?, 'active', 1, ?, ?)",
                (batch_id, key, generation, item["status"], item["evidence"],
                 revision, revision),
            )
            _event(db, batch_id, revision,
                   "item_added" if key in plan["added"] else "item_replaced",
                   item_key=key,
                   detail={"generation": generation, "status": item["status"],
                           "correction": digest}, now=now)
            done += 1
            _maybe_fail(fail_after, done)

        db.execute(
            "UPDATE batch_corrections SET state='applied', revision=?"
            " WHERE batch_id=? AND digest=?", (revision, batch_id, digest),
        )
        db.execute(
            "UPDATE batches SET revision=?, status=?, updated_at=? WHERE batch_id=?",
            (revision, "corrected" if changed else batch["status"], now, batch_id),
        )
        _event(db, batch_id, revision, "correction_applied",
               detail={"correction": digest, "reason": row["reason"],
                       "plan": plan, "changed": changed}, now=now)
        _maybe_fail(fail_after, done + 1)
        return _detail(db, _batch_row(db, batch_id))


def _maybe_fail(fail_after, done):
    """测试用的崩溃注入点；正常运行时 fail_after 为 None。"""
    if fail_after is not None and done >= fail_after:
        raise RuntimeError("injected failure after step %d" % done)


def recover(store, batch_id: str | None = None) -> list[dict]:
    """续做 pending 更正单；已提交的更正与冲正不会重复。"""
    with store.read() as db:
        sql = ("SELECT batch_id, digest FROM batch_corrections WHERE state='pending'")
        params: tuple = ()
        if batch_id is not None:
            sql += " AND batch_id=?"
            params = (batch_id,)
        rows = db.execute(sql + " ORDER BY created_at, batch_id", params).fetchall()
    recovered = []
    for row in rows:
        try:
            _apply_pending(store, row["batch_id"], row["digest"])
            recovered.append({"batch_id": row["batch_id"], "status": "recovered"})
        except DomainError as error:
            recovered.append({"batch_id": row["batch_id"], "status": "stale",
                              "error": error.code})
        except Exception as error:  # 仍失败：保留 pending，下次可再续做
            recovered.append({"batch_id": row["batch_id"], "status": "retryable",
                              "error": str(error)})
    return recovered


# ---------------------------------------------------------------- 导出

def export_rows(store, batch_id: str, include_unclaimed: bool = False) -> list[dict]:
    """导出行 = 当前修订的领取回执；内容取自回执快照，与领取时一致。"""
    recover(store, batch_id)
    with store.read() as db:
        _batch_row(db, batch_id)
        rows = db.execute(
            "SELECT i.item_key, i.status, i.evidence, i.revision_added,"
            " r.receipt_id, r.status AS receipt_status, r.evidence AS receipt_evidence,"
            " r.state, r.claimed_by, r.confirmed_by"
            " FROM batch_items i LEFT JOIN receipts r"
            " ON r.batch_id=i.batch_id AND r.item_key=i.item_key"
            " AND r.item_generation=i.item_generation AND r.active=1"
            " WHERE i.batch_id=? AND i.active=1"
            " ORDER BY i.item_key",
            (batch_id,),
        ).fetchall()
    out = []
    for row in rows:
        if row["receipt_id"] is None and not include_unclaimed:
            continue
        out.append({
            "receipt_id": row["receipt_id"] or "",
            "key": row["item_key"],
            # 有无回执都导出当前项内容；有回执时与回执快照一致
            "status": row["receipt_status"] if row["receipt_id"] else row["status"],
            "evidence": row["receipt_evidence"] if row["receipt_id"] else row["evidence"],
            "receipt_state": row["state"] or "unclaimed",
            "revision": row["revision_added"],
            "claimed_by": row["claimed_by"] or "",
            "confirmed_by": row["confirmed_by"] or "",
        })
    return out


EXPORT_FIELDS = ["receipt_id", "key", "status", "evidence", "receipt_state", "revision",
                 "claimed_by", "confirmed_by"]


def export_csv(store, batch_id: str, include_unclaimed: bool = False) -> str:
    rows = export_rows(store, batch_id, include_unclaimed)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=EXPORT_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()
