"""M6 批次回执与冲正：按领取回执更正，而不是重写差异文件。"""
import csv
import io
import threading

import pytest

from app.modules.m6_runs import batch as rules
from app.modules.m6_runs import service as ledger


def items(*pairs):
    return [{"key": key, "status": status, "evidence": key + "-ev"} for key, status in pairs]


def make(client, rows):
    response = client.post("/api/batches", json={"items": rows})
    assert response.status_code == 200, response.text
    return response.json()


def claim(client, batch_id, worker="w1", **extra):
    response = client.post(f"/api/batches/{batch_id}/claim",
                           json={"worker": worker, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def confirm(client, batch_id, worker="w1", **extra):
    response = client.post(f"/api/batches/{batch_id}/confirm",
                           json={"worker": worker, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def correct(client, batch_id, rows, **extra):
    response = client.post(f"/api/batches/{batch_id}/correct",
                           json={"items": rows, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def detail(client, batch_id):
    response = client.get(f"/api/batches/{batch_id}")
    assert response.status_code == 200, response.text
    return response.json()


def export_rows(client, batch_id):
    response = client.get(f"/api/batches/{batch_id}/export.csv")
    assert response.status_code == 200
    return list(csv.DictReader(io.StringIO(response.text)))


def raw_batch(client, batch_id):
    """不经 recover 直接读取，用于观察写到一半时的持久状态。"""
    store = client.app.state.store
    with store.read() as db:
        return ledger._detail(db, ledger._batch_row(db, batch_id))


def test_batch_id_is_content_derived_and_replayable(client):
    first = make(client, items(("a", "MISSING"), ("b", "ORPHAN")))
    second = make(client, items(("b", "ORPHAN"), ("a", "MISSING")))
    assert first["batch_id"] == second["batch_id"]
    assert second["replayed"] is True
    assert first["item_count"] == 2


def test_claim_issues_receipt_and_replays_same_worker(client):
    batch = make(client, items(("a", "MISSING"), ("b", "ORPHAN")))
    first = claim(client, batch["batch_id"])
    assert len(first["issued"]) == 2
    again = claim(client, batch["batch_id"])
    assert again["issued"] == []
    assert {row["receipt_id"] for row in again["replayed"]} == \
           {row["receipt_id"] for row in first["issued"]}
    contended = claim(client, batch["batch_id"], worker="w2")
    assert len(contended["contended"]) == 2


def test_exported_rows_match_receipts(client):
    batch = make(client, items(("a", "MISSING"), ("b", "ORPHAN")))
    assert export_rows(client, batch["batch_id"]) == []
    receipts = claim(client, batch["batch_id"])["issued"]
    rows = export_rows(client, batch["batch_id"])
    assert {row["receipt_id"] for row in rows} == {row["receipt_id"] for row in receipts}
    assert all(row["receipt_state"] == "claimed" for row in rows)
    by_key = {row["key"]: row for row in receipts}
    for row in rows:
        assert row["status"] == by_key[row["key"]]["status"]
        assert row["evidence"] == by_key[row["key"]]["evidence"]
    unclaimed = export_rows(client, batch["batch_id"])
    assert len(unclaimed) == 2


def test_unconfirmed_withdrawn_item_is_superseded_without_reversal(client):
    batch = make(client, items(("a", "MISSING"), ("b", "ORPHAN")))
    batch_id = batch["batch_id"]
    receipts = {row["key"]: row["receipt_id"] for row in claim(client, batch_id)["issued"]}
    confirm(client, batch_id, receipt_ids=[receipts["a"]])
    result = correct(client, batch_id, items(("a", "MISSING")))
    assert result["revision"] == 2
    assert result["progress"]["withdrawn"] == 1
    assert result["progress"]["reversals"] == 0
    history = {row["key"]: row for row in result["receipt_history"]}
    assert history["b"]["state"] == "superseded"
    assert history["b"]["active"] is False
    # 已确认的 a 原凭据保留，且没有任何冲正
    assert result["receipts"][0]["key"] == "a"
    assert result["receipts"][0]["receipt_id"] == receipts["a"]
    assert result["reversals"] == []


def test_confirmed_withdrawn_item_keeps_receipt_and_reverses_once(client):
    batch = make(client, items(("a", "MISSING"), ("b", "ORPHAN")))
    batch_id = batch["batch_id"]
    receipts = {row["key"]: row["receipt_id"] for row in claim(client, batch_id)["issued"]}
    confirm(client, batch_id)
    result = correct(client, batch_id, items(("b", "ORPHAN")), reason="撤销误报 a")
    assert result["revision"] == 2
    assert [row["key"] for row in result["items"]] == ["b"]
    assert len(result["reversals"]) == 1
    reversal = result["reversals"][0]
    assert reversal["receipt_id"] == receipts["a"]
    assert reversal["reason"] == "WITHDRAW_CONFIRMED_ITEM"
    # 原凭据仍在历史里，内容未被改写
    history = {row["key"]: row for row in result["receipt_history"]}
    assert history["a"]["state"] == "confirmed"
    assert history["a"]["evidence"] == "a-ev"
    # 再次提交同一新清单：重放，不新增冲正
    replay = correct(client, batch_id, items(("b", "ORPHAN")), reason="撤销误报 a")
    assert replay["replayed"] is True
    assert len(replay["reversals"]) == 1


def test_replaced_confirmed_item_reverses_then_new_generation(client):
    batch = make(client, items(("a", "MISSING")))
    batch_id = batch["batch_id"]
    old = claim(client, batch_id)["issued"][0]
    confirm(client, batch_id)
    result = correct(client, batch_id, items(("a", "ORPHAN")))
    assert result["progress"]["replaced"] == 1
    assert [row["key"] for row in result["items"]] == ["a"]
    assert result["items"][0]["status"] == "ORPHAN"
    assert result["items"][0]["generation"] == 2
    assert [row["receipt_id"] for row in result["reversals"]] == [old["receipt_id"]]
    # 替换后的项没有旧回执，必须重新领取
    fresh = claim(client, batch_id)
    assert len(fresh["issued"]) == 1
    assert fresh["issued"][0]["receipt_id"] != old["receipt_id"]
    assert fresh["issued"][0]["evidence"] == "a-ev"


def test_late_confirmation_of_superseded_receipt_only_reverses_that_receipt(client):
    batch = make(client, items(("a", "MISSING"), ("b", "ORPHAN")))
    batch_id = batch["batch_id"]
    receipts = {row["key"]: row["receipt_id"] for row in claim(client, batch_id)["issued"]}
    result = correct(client, batch_id, items(("b", "ORPHAN")))
    assert result["reversals"] == []
    late = confirm(client, batch_id, worker="w2", receipt_ids=[receipts["a"]])
    assert [row["key"] for row in late["late_confirmed"]] == ["a"]
    assert late["late_confirmed"][0]["late_confirmed"] is True
    after = detail(client, batch_id)
    # 新修订的项、进度与导出都不受乱序确认影响
    assert after["revision"] == 2
    assert [row["key"] for row in after["items"]] == ["b"]
    assert after["progress"]["confirmed"] == 0
    assert [row["key"] for row in after["reversals"]] == ["a"]
    assert after["reversals"][0]["reason"] == "LATE_CONFIRMATION"
    rows = export_rows(client, batch_id)
    assert rows == [] or all(row["key"] != "a" for row in rows)
    # 重复乱序确认不新增冲正
    confirm(client, batch_id, worker="w2", receipt_ids=[receipts["a"]])
    assert len(detail(client, batch_id)["reversals"]) == 1


def test_correction_crash_is_recoverable_and_retry_adds_no_reversal(client, monkeypatch):
    batch = make(client, items(("a", "MISSING"), ("b", "ORPHAN")))
    batch_id = batch["batch_id"]
    claim(client, batch_id)
    confirm(client, batch_id)

    original = ledger._maybe_fail

    def crash(fail_after, done):
        if done >= 1:
            raise RuntimeError("simulated crash")
        return original(fail_after, done)

    monkeypatch.setattr(ledger, "_maybe_fail", crash)
    with pytest.raises(RuntimeError):
        client.post(f"/api/batches/{batch_id}/correct",
                    json={"items": items(("a", "MISSING"))})
    monkeypatch.setattr(ledger, "_maybe_fail", original)

    broken = raw_batch(client, batch_id)
    assert broken["pending_correction_count"] == 1
    assert broken["revision"] == 1
    assert broken["reversals"] == []
    assert len(broken["items"]) == 2

    recovered = client.post(f"/api/batches/{batch_id}/recover")
    assert recovered.status_code == 200
    assert recovered.json()["recovered"][0]["status"] == "recovered"
    after = detail(client, batch_id)
    assert after["revision"] == 2
    assert after["pending_correction_count"] == 0
    assert len(after["reversals"]) == 1
    assert [row["key"] for row in after["items"]] == ["a"]

    # 重试同一新清单不新增冲正
    replay = correct(client, batch_id, items(("a", "MISSING")))
    assert len(replay["reversals"]) == 1
    assert replay["revision"] == 2


def test_stale_revision_rejected_but_events_kept(client):
    batch = make(client, items(("a", "MISSING")))
    batch_id = batch["batch_id"]
    claim(client, batch_id)
    correct(client, batch_id, items(("a", "ORPHAN")))
    response = client.post(f"/api/batches/{batch_id}/correct",
                           json={"items": items(("a", "MISSING")),
                                 "expected_revision": 1})
    assert response.status_code == 422
    assert response.json()["error"] == "STALE_VERSION"
    after = detail(client, batch_id)
    assert any(row["type"] == "correction_stale" for row in after["events"])
    assert after["revision"] == 2


def test_claim_and_correction_concurrently_stay_traceable(client):
    batch = make(client, [{"key": f"k{index}", "status": "MISSING", "evidence": "e"}
                          for index in range(12)])
    batch_id = batch["batch_id"]
    store = client.app.state.store
    errors = []

    def worker(name):
        try:
            for _ in range(4):
                ledger.claim(store, batch_id, {"worker": name})
        except Exception as error:  # pragma: no cover - 失败时给出可读诊断
            errors.append(error)

    def reviewer():
        try:
            ledger.correct(store, batch_id,
                           {"items": [{"key": f"k{index}", "status": "MISSING",
                                       "evidence": "e"} for index in range(8)]})
        except Exception as error:  # pragma: no cover
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(f"w{index}",)) for index in range(3)]
    threads.append(threading.Thread(target=reviewer))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    after = detail(client, batch_id)
    active = {(row["key"], row["generation"]) for row in after["items"]}
    assert len(after["items"]) == 8
    # 有效回执只挂在有效项上，且每项至多一条
    pairs = [(row["key"], row["generation"]) for row in after["receipts"]]
    assert all(pair in active for pair in pairs)
    assert len(pairs) == len(set(pairs))
    # 被撤销 / 替换项的凭据要么作废留档，要么已冲正，二者都可追溯
    reversed_ids = {row["receipt_id"] for row in after["reversals"]}
    for row in after["receipt_history"]:
        assert row["active"] is False
        assert row["state"] in ("superseded", "confirmed") or \
            row["receipt_id"] in reversed_ids
    # 撤销与领取都留有事件，可按序号追溯
    assert any(row["type"] in ("receipt_superseded", "item_withdrawn", "item_replaced")
               for row in after["events"])
    assert any(row["type"] in ("claimed", "claim_contended") for row in after["events"])
    sequences = [row["seq"] for row in after["events"]]
    assert sequences == sorted(sequences)
    exported = export_rows(client, batch_id)
    assert {row["receipt_id"] for row in exported} == \
           {row["receipt_id"] for row in after["receipts"]}


def test_pure_rules_prefer_receipt_state():
    current = [
        {"key": "a", "status": "MISSING", "evidence": "x"},
        {"key": "b", "status": "ORPHAN", "evidence": "y"},
        {"key": "c", "status": "MISSING", "evidence": "z"},
    ]
    wanted = [
        {"key": "a", "status": "MISSING", "evidence": "x"},
        {"key": "c", "status": "MISSING", "evidence": "z2"},
        {"key": "d", "status": "MISSING", "evidence": "w"},
    ]
    plan = rules.diff_plan(current, wanted)
    assert plan == {"withdrawn": ["b"], "added": ["d"],
                    "replaced": ["c"], "unchanged": ["a"]}


def test_batch_page_shows_receipts_and_progress(client):
    batch = make(client, items(("a", "MISSING"), ("b", "ORPHAN")))
    batch_id = batch["batch_id"]
    receipts = {row["key"]: row["receipt_id"] for row in claim(client, batch_id)["issued"]}
    confirm(client, batch_id)
    correct(client, batch_id, items(("a", "MISSING")), reason="误报 b")
    page = client.get(f"/batches/{batch_id}")
    assert page.status_code == 200
    assert receipts["a"] in page.text
    assert "WITHDRAW_CONFIRMED_ITEM" in page.text
    assert "更正进度" in page.text
    runs = client.get("/runs")
    assert runs.status_code == 200
    assert batch_id in runs.text


def test_unclaimed_items_are_never_exported_by_default(client):
    batch = make(client, items(("a", "MISSING"), ("b", "ORPHAN")))
    batch_id = batch["batch_id"]
    assert export_rows(client, batch_id) == []
    page = client.get(f"/api/batches/{batch_id}/export.csv?include_unclaimed=true")
    assert page.status_code == 200
    rows = list(csv.DictReader(io.StringIO(page.text)))
    assert {row["receipt_state"] for row in rows} == {"unclaimed"}
    assert all(row["receipt_id"] == "" for row in rows)


def test_invalid_batches_are_rejected(client):
    assert client.post("/api/batches", json={"items": []}).status_code == 422
    duplicate = client.post("/api/batches", json={
        "items": [{"key": "a", "status": "MISSING"},
                  {"key": "a", "status": "ORPHAN"}]})
    assert duplicate.status_code == 422
    assert duplicate.json()["error"] == "DUPLICATE_KEY"
    assert client.post("/api/batches/nope/claim",
                       json={"worker": "w"}).json()["error"] == "BATCH_NOT_FOUND"
    batch = make(client, items(("a", "MISSING")))
    missing_worker = client.post(f"/api/batches/{batch['batch_id']}/claim", json={})
    assert missing_worker.status_code == 422
    assert missing_worker.json()["error"] == "WORKER_REQUIRED"
