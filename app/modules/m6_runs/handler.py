from fastapi import APIRouter, Body, Request
from fastapi.responses import Response

from . import core, service

router = APIRouter(tags=["M6-批次导出"])


def _live_batch(request: Request) -> str | None:
    """页面默认展示最近一个批次；没有批次时展示样例批次说明。"""
    batches = service.list_batches(request.app.state.store)
    return batches[-1]["batch_id"] if batches else None


@router.get("/runs")
def page(request: Request):
    store = request.app.state.store
    legacy = core.run({"store": store})
    batch_id = _live_batch(request)
    batch = service.get_batch(store, batch_id) if batch_id else None
    return request.app.state.templates.TemplateResponse(
        request,
        "m6_runs.html",
        {
            "title": "批次导出",
            "data": legacy,
            "batch_id": batch_id,
            "batch": batch,
            "batches": service.list_batches(store),
        },
    )


@router.get("/batches/{batch_id}")
def batch_page(batch_id: str, request: Request):
    store = request.app.state.store
    batch = service.get_batch(store, batch_id)
    return request.app.state.templates.TemplateResponse(
        request,
        "m6_runs.html",
        {
            "title": "批次导出",
            "data": core.run({"store": store}),
            "batch_id": batch_id,
            "batch": batch,
            "batches": service.list_batches(store),
        },
    )


@router.get("/api/runs")
def runs(request: Request):
    core.run({"store": request.app.state.store})
    return {"runs": request.app.state.store.all()}


@router.get("/api/export/differences.csv")
def export(request: Request):
    data = core.run({"store": request.app.state.store})
    return Response(data["csv"], media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=evidence.csv"})


@router.post("/api/batches")
def create_batch(request: Request, payload: dict = Body(default={})):
    return service.create_batch(request.app.state.store, payload or {})


@router.get("/api/batches")
def list_batches(request: Request):
    return {"batches": service.list_batches(request.app.state.store)}


@router.get("/api/batches/{batch_id}")
def get_batch(batch_id: str, request: Request):
    return service.get_batch(request.app.state.store, batch_id)


@router.post("/api/batches/{batch_id}/claim")
def claim(batch_id: str, request: Request, payload: dict = Body(default={})):
    return service.claim(request.app.state.store, batch_id, payload or {})


@router.post("/api/batches/{batch_id}/confirm")
def confirm(batch_id: str, request: Request, payload: dict = Body(default={})):
    return service.confirm(request.app.state.store, batch_id, payload or {})


@router.post("/api/batches/{batch_id}/correct")
def correct(batch_id: str, request: Request, payload: dict = Body(default={})):
    return service.correct(request.app.state.store, batch_id, payload or {})


@router.post("/api/batches/{batch_id}/recover")
def recover(batch_id: str, request: Request):
    return {"recovered": service.recover(request.app.state.store, batch_id),
            "batch": service.get_batch(request.app.state.store, batch_id)}


@router.get("/api/batches/{batch_id}/export.csv")
def batch_export(batch_id: str, request: Request, include_unclaimed: bool = False):
    body = service.export_csv(request.app.state.store, batch_id, include_unclaimed)
    return Response(body, media_type="text/csv",
                    headers={"Content-Disposition":
                             f"attachment; filename={batch_id}.csv"})
