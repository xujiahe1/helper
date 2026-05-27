"""读 git repo + sqlite 渲染极简 HTML(无 JS / 无前端框架)。"""

from __future__ import annotations

import json
import re
from html import escape

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from helper.compiler import current_bundle_version, load_bundle
from helper.storage import session
from helper.storage.models import (
    AskAnswer,
    ConflictLog,
    InquiryLog,
    L1Result,
    RawInput,
    SpecCandidate,
)

_STYLE = """<style>
body { font-family: -apple-system, sans-serif; max-width: 880px; margin: 2em auto; padding: 0 1em; color: #222; }
h1 { border-bottom: 2px solid #333; padding-bottom: .3em; }
h2 { color: #555; margin-top: 2em; }
table { border-collapse: collapse; width: 100%; margin: .5em 0; }
th, td { padding: .4em .6em; border-bottom: 1px solid #ddd; text-align: left; vertical-align: top; }
.tag { display:inline-block; padding: 1px 6px; background: #eef; border-radius: 3px; font-size: 90%; }
pre { background: #f6f6f6; padding: .8em; overflow-x: auto; border-radius: 4px; }
a { color: #06c; }
.meta { color: #888; font-size: 90%; }
</style>"""


def _md_to_html(md: str) -> str:
    out = []
    for line in md.splitlines():
        if line.startswith("# "):
            out.append(f"<h1>{escape(line[2:])}</h1>")
        elif line.startswith("## "):
            out.append(f"<h2>{escape(line[3:])}</h2>")
        elif line.startswith("- "):
            out.append(f"<li>{escape(line[2:])}</li>")
        elif not line.strip():
            out.append("<br/>")
        else:
            out.append(f"<p>{escape(line)}</p>")
    return "\n".join(out)


def _html(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><meta charset='utf-8'><title>{escape(title)}</title>"
        f"{_STYLE}<body>{body}</body>"
    )


def build_browser_router() -> APIRouter:
    r = APIRouter(prefix="/browse")

    @r.get("", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        bundle = load_bundle()
        entities = bundle.get("entities", [])
        specs = bundle.get("specs", [])
        body = [f"<h1>Helper 知识库</h1>",
                f"<p class='meta'>bundle version: {escape(current_bundle_version())}</p>",
                f"<h2>Specs ({len(specs)})</h2><table>"]
        for sp in specs:
            slug = sp.get("slug", "")
            title = sp.get("title", slug)
            body.append(f"<tr><td><a href='/admin/browse/specs/{escape(slug)}'>{escape(slug)}</a></td>"
                        f"<td>{escape(title)}</td></tr>")
        body.append("</table>")

        body.append(f"<h2>Entities ({len(entities)})</h2><table>")
        for e in entities:
            slug = e.get("slug", "")
            name = e.get("name", slug)
            etype = e.get("entity_type", "")
            body.append(f"<tr><td><a href='/admin/browse/entities/{escape(slug)}'>{escape(slug)}</a></td>"
                        f"<td>{escape(name)}</td><td><span class='tag'>{escape(etype)}</span></td></tr>")
        body.append("</table>")

        # Recent raw
        body.append("<h2>最近 raw</h2><table>")
        with session() as s:
            from sqlalchemy import select
            rows = s.execute(select(RawInput).order_by(RawInput.id.desc()).limit(20)).scalars().all()
            for r0 in rows:
                preview = (r0.content_text or "").replace("\n", " ")[:80]
                body.append(f"<tr><td><a href='/admin/browse/raw/{r0.id}'>raw#{r0.id}</a></td>"
                            f"<td>{escape(r0.author_domain or '')}</td>"
                            f"<td>{escape(preview)}</td></tr>")
        body.append("</table>")

        # Conflicts open
        with session() as s:
            from sqlalchemy import select
            open_conflicts = s.execute(
                select(ConflictLog).where(ConflictLog.resolution == "open").limit(20)
            ).scalars().all()
        if open_conflicts:
            body.append(f"<h2>待裁决冲突 ({len(open_conflicts)})</h2><table>")
            for c in open_conflicts:
                body.append(f"<tr><td>#{c.id}</td><td>{escape(c.spec_slug)}</td>"
                            f"<td><span class='tag'>{escape(c.severity)}</span></td>"
                            f"<td>{escape((c.summary or '')[:100])}</td></tr>")
            body.append("</table>")
        return _html("Helper 知识库", "\n".join(body))

    @r.get("/specs/{slug}", response_class=HTMLResponse)
    def show_spec(slug: str) -> HTMLResponse:
        bundle = load_bundle()
        for sp in bundle.get("specs", []):
            if sp.get("slug") == slug:
                body_md = sp.get("_body", "")
                meta_lines = [f"<p class='meta'>review: {escape(str(sp.get('review_status', '')))} · "
                              f"refs: {escape(str(sp.get('raw_refs', [])))}</p>"]
                return _html(f"spec / {slug}", "\n".join(meta_lines) + _md_to_html(body_md))
        raise HTTPException(status_code=404, detail=f"spec {slug} not found")

    @r.get("/entities/{slug}", response_class=HTMLResponse)
    def show_entity(slug: str) -> HTMLResponse:
        bundle = load_bundle()
        for e in bundle.get("entities", []):
            if e.get("slug") == slug:
                body_md = e.get("_body", "")
                meta_lines = [
                    f"<p class='meta'>type: {escape(str(e.get('entity_type', '')))} · "
                    f"mentions: {escape(str(e.get('mention_count', 0)))} · "
                    f"refs: {escape(str(e.get('raw_refs', [])))}</p>"
                ]
                return _html(f"entity / {slug}", "\n".join(meta_lines) + _md_to_html(body_md))
        raise HTTPException(status_code=404, detail=f"entity {slug} not found")

    @r.get("/raw/{raw_id}", response_class=HTMLResponse)
    def show_raw(raw_id: int) -> HTMLResponse:
        from helper.storage.l1_view import list_l1_atoms

        with session() as s:
            raw = s.get(RawInput, raw_id)
            if raw is None:
                raise HTTPException(status_code=404)
            l1 = s.get(L1Result, raw_id)
            atoms = list_l1_atoms(s, raw_id)
            body = [f"<h1>raw#{raw_id}</h1>",
                    f"<p class='meta'>{escape(raw.source_type)} · {escape(raw.author_domain or '')} · "
                    f"{raw.created_at:%Y-%m-%d %H:%M}</p>",
                    f"<pre>{escape(raw.content_text or '')}</pre>"]
            if l1 is not None and l1.error:
                body.append(f"<h2>L1 ERROR</h2><pre>{escape(l1.error)}</pre>")
            elif atoms:
                body.append(f"<h2>L1 atoms ({len(atoms)})</h2>")
                for a in atoms:
                    body.append(
                        f"<h3>idx={a['idx']} <span class='tag'>{escape(a['type'])}</span></h3>"
                        "<table>"
                    )
                    for k, v in a["payload"].items():
                        if isinstance(v, (dict, list)):
                            v_str = json.dumps(v, ensure_ascii=False, indent=2)
                            body.append(
                                f"<tr><th>{escape(str(k))}</th>"
                                f"<td><pre>{escape(v_str)}</pre></td></tr>"
                            )
                        else:
                            body.append(
                                f"<tr><th>{escape(str(k))}</th>"
                                f"<td>{escape(str(v))}</td></tr>"
                            )
                    body.append("</table>")
            elif l1 is not None:
                body.append("<h2>L1</h2><p class='meta'>(no atoms — likely filtered or empty)</p>")
        return _html(f"raw#{raw_id}", "\n".join(body))

    return r
