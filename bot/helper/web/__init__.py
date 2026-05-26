"""Surface 3 — 只读 Web 知识库浏览(渲染 git spec repo)。

挂在 admin router 下:
  GET /admin/browse                 — 索引页 (entities + specs)
  GET /admin/browse/entities/<slug> — 单条 entity
  GET /admin/browse/specs/<slug>    — 单条 spec
  GET /admin/browse/raw/<id>        — raw + L1 详情
"""

from helper.web.browser import build_browser_router

__all__ = ["build_browser_router"]
