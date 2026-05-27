"""FastAPI app — bot 对外 HTTP 入口。

公网路由(无鉴权):
  GET  /healthz   — 存活探针
  POST /callback  — Wave IM 回调(签名 + AES + event_id 去重,见 helper/im/wave_webhook.py)
                    仅当 wave_callback_configured 时挂上;否则连路由都不注册,
                    防止部署没配 key 也悄无声息收事件

Admin 路由 /admin/*(由 HELPER_ADMIN_SK 保护):
  GET  /admin/healthz — 鉴权链路自测
  GET  /admin/raw-inputs — 列最近的 raw + L1 状态
  GET  /admin/raw-inputs/{id} — 单条详情(完整 envelope + L1 结果)
  POST /admin/l1-backfill — 重跑缺 L1 / L1 失败的 raw_inputs

设计:
- HELPER_ADMIN_SK 为空时,/admin/* 整体**不注册**,探测返 404(不暴露探测面)
- HELPER_ADMIN_SK 配了的话,所有 /admin/* 都通过 Depends 校验 X-Helper-Admin-Key header
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException

from helper import __version__
from helper.config import get_settings


def _require_admin_sk(
    x_helper_admin_key: str | None = Header(default=None, alias="X-Helper-Admin-Key"),
) -> None:
    s = get_settings()
    # 双保险:即便有人手工挂上 admin router,sk 没配也拒
    if not s.admin_enabled:
        raise HTTPException(status_code=404)
    if x_helper_admin_key != s.helper_admin_sk:
        raise HTTPException(status_code=401, detail="unauthorized")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="helper", version=__version__)

    @app.on_event("startup")
    async def _on_startup() -> None:
        # APScheduler 接管定时任务扫描。模块级懒导入,避免测试 / CLI 时强引入 apscheduler。
        from helper.scheduler import start_scheduler

        start_scheduler()

    @app.on_event("shutdown")
    async def _on_shutdown() -> None:
        from logging import getLogger

        from helper.im.queue import drain
        from helper.scheduler import stop_scheduler

        stop_scheduler()
        leftover = await drain(timeout=5.0)
        if leftover:
            getLogger(__name__).warning(
                "shutdown: %d background tasks still running after drain", leftover
            )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    if settings.wave_callback_configured:
        from helper.im.wave_webhook import router as wave_router

        app.include_router(wave_router)

    if settings.admin_enabled:
        admin = APIRouter(prefix="/admin", dependencies=[Depends(_require_admin_sk)])

        @admin.get("/healthz")
        def admin_healthz() -> dict[str, Any]:
            return {"status": "ok", "admin": True, "version": __version__}

        @admin.get("/raw-inputs")
        def list_raw(limit: int = 20) -> dict[str, Any]:
            import json as _json

            from helper.storage import raw_store, session
            from helper.storage.models import L1Result

            limit = max(1, min(limit, 200))
            with session() as sess:
                rows = raw_store.list_recent(sess, limit=limit)
                items = []
                for r in rows:
                    l1 = sess.get(L1Result, r.id)
                    items.append(
                        {
                            "id": r.id,
                            "source_type": r.source_type,
                            "source_ref": r.source_ref,
                            "author": r.author_domain,
                            "created_at": r.created_at.isoformat(),
                            "processed": r.processed,
                            "preview": r.content_text[:120],
                            "l1": (
                                None
                                if l1 is None
                                else {
                                    "status": "error" if l1.error else "ok",
                                    "error": l1.error or None,
                                    "model": l1.model,
                                }
                            ),
                        }
                    )
            return {"items": items, "count": len(items)}

        @admin.get("/raw-inputs/{raw_id}")
        def show_raw(raw_id: int) -> dict[str, Any]:
            from helper.storage import session
            from helper.storage.l1_view import list_l1_atoms
            from helper.storage.models import L1Result, RawInput

            with session() as sess:
                raw = sess.get(RawInput, raw_id)
                if raw is None:
                    raise HTTPException(status_code=404, detail=f"raw#{raw_id} not found")
                l1 = sess.get(L1Result, raw_id)
                atoms = list_l1_atoms(sess, raw_id)
                return {
                    "raw": {
                        "id": raw.id,
                        "source_type": raw.source_type,
                        "source_ref": raw.source_ref,
                        "author": raw.author_domain,
                        "created_at": raw.created_at.isoformat(),
                        "processed": raw.processed,
                        "content_text": raw.content_text,
                        "attachments_json": raw.attachments_json,
                    },
                    "l1": (
                        None
                        if l1 is None
                        else {
                            "error": l1.error or None,
                            "model": l1.model,
                            "created_at": l1.created_at.isoformat(),
                            "atoms": atoms,
                        }
                    ),
                }

        @admin.post("/l1-backfill")
        def post_l1_backfill(limit: int = 50) -> dict[str, Any]:
            from helper.ingest import backfill_pending

            limit = max(1, min(limit, 200))
            done = backfill_pending(limit=limit)
            return {"backfilled": len(done), "raw_ids": done}

        @admin.post("/promote-entities")
        def post_promote_entities(limit: int = 50) -> dict[str, Any]:
            from helper.ontology import promote_eligible

            promoted = promote_eligible(limit=limit)
            return {"promoted": promoted, "count": len(promoted)}

        @admin.post("/specgen/run")
        def post_specgen_run() -> dict[str, Any]:
            from helper.specgen import cluster_l1_results, draft_spec_from_cluster

            clusters = cluster_l1_results(min_cluster_size=2)
            drafted = []
            for c in clusters[:10]:
                sc = draft_spec_from_cluster(c)
                if sc is not None:
                    drafted.append({"slug": sc.slug, "title": sc.title, "raws": c})
            return {"clusters": len(clusters), "drafted": drafted}

        @admin.post("/specs/{slug}/promote")
        def post_promote_spec(slug: str, reviewer: str = "") -> dict[str, Any]:
            from helper.specgen import promote_spec

            path = promote_spec(slug, reviewer=reviewer)
            if path is None:
                raise HTTPException(status_code=404, detail=f"spec candidate {slug} not found")
            return {"slug": slug, "git_path": path}

        @admin.post("/conflicts/{log_id}/resolve")
        def post_resolve_conflict(log_id: int, resolution: str, resolver: str = "") -> dict[str, Any]:
            from helper.conflict import resolve

            ok = resolve(log_id, resolution=resolution, resolver_domain=resolver)
            if not ok:
                raise HTTPException(status_code=404, detail=f"conflict {log_id} not found")
            return {"log_id": log_id, "resolution": resolution}

        @admin.get("/conflicts")
        def list_conflicts(status: str = "open", limit: int = 50) -> dict[str, Any]:
            from sqlalchemy import select

            from helper.storage import session as _sess
            from helper.storage.models import ConflictLog

            limit = max(1, min(limit, 200))
            with _sess() as s:
                q = select(ConflictLog).order_by(ConflictLog.created_at.desc()).limit(limit)
                if status != "all":
                    q = q.where(ConflictLog.resolution == status)
                rows = s.execute(q).scalars().all()
                items = [
                    {
                        "id": r.id,
                        "raw_id": r.raw_id,
                        "spec_slug": r.spec_slug,
                        "summary": r.summary,
                        "severity": r.severity,
                        "resolution": r.resolution,
                        "created_at": r.created_at.isoformat(),
                    }
                    for r in rows
                ]
            return {"items": items, "count": len(items)}

        @admin.post("/ask")
        def post_ask(payload: dict[str, Any]) -> dict[str, Any]:
            from helper.ask import ask

            q = str(payload.get("question", "")).strip()
            if not q:
                raise HTTPException(status_code=400, detail="missing 'question'")
            asker = str(payload.get("asker", "admin"))
            ans = ask(q, asker_domain=asker)
            return {
                "answer": ans.answer,
                "confidence": ans.confidence,
                "citations": ans.citations,
                "bundle_version": ans.bundle_version,
                "model": ans.model,
                "answer_id": ans.answer_id,
            }

        @admin.post("/inbox/send-weekly")
        def post_send_weekly(receiver: str, receiver_id_type: str = "user_id") -> dict[str, Any]:
            from helper.inbox import send_to

            ok = send_to(receiver, receiver_id_type=receiver_id_type)
            return {"sent": ok}

        # Surface 3 — read-only browser (HTML)
        from helper.web import build_browser_router
        admin.include_router(build_browser_router())

        app.include_router(admin)

    return app


# uvicorn 入口: `uvicorn helper.server:app`
app = create_app()
