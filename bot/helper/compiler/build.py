"""把 git spec repo 编译成单个 bundle.json,带版本(commit sha)。

bundle 结构:
{
  "version": "<commit sha 短码>",
  "built_at": iso,
  "entities": [{slug, name, entity_type, description, ...frontmatter, body}],
  "specs":    [{slug, title, statement, rationale, ...frontmatter, body}]
}

写到 var/helper/bundles/<sha>.json,latest.json 软链 / 直接复制最新一份。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from git import Repo

from helper.config import get_settings

_FM_RE = re.compile(r"^---\n(.+?)\n---\n(.*)$", re.DOTALL)


def _split_md(text: str) -> tuple[dict, str]:
    """frontmatter + body。无 frontmatter 时返 ({}, text)。"""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    fm = yaml.safe_load(m.group(1)) or {}
    return (fm if isinstance(fm, dict) else {}), m.group(2)


def _json_safe(obj: Any) -> Any:
    """yaml load 出来可能是 datetime/date,转 ISO 字符串供 JSON 序列化。"""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


def _scan(reldir: Path, abs_root: Path) -> list[dict]:
    out = []
    d = abs_root / reldir
    if not d.exists():
        return out
    for f in sorted(d.glob("*.md")):
        if f.name.startswith("."):
            continue
        text = f.read_text(encoding="utf-8")
        fm, body = _split_md(text)
        fm = _json_safe(fm) if isinstance(fm, dict) else {}
        fm["_body"] = body.strip()
        fm["_path"] = str(f.relative_to(abs_root))
        out.append(fm)
    return out


def current_bundle_version() -> str:
    """spec repo 当前 HEAD commit 的短 sha。"""
    s = get_settings()
    repo = Repo(s.helper_spec_git_dir)
    return repo.head.commit.hexsha[:12]


def build_bundle() -> Path:
    """读 git spec repo → 写 var/helper/bundles/<sha>.json + latest.json。

    返回写入的 latest.json 路径。
    """
    s = get_settings()
    repo_dir = s.helper_spec_git_dir
    sha = current_bundle_version()

    bundle = {
        "version": sha,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "entities": _scan(Path("ontology") / "entities", repo_dir),
        "specs": _scan(Path("specs"), repo_dir),
        "facts": _scan(Path("facts"), repo_dir),
        "cases": _scan(Path("cases"), repo_dir),
    }

    out_dir = s.helper_data_dir / "bundles"
    out_dir.mkdir(parents=True, exist_ok=True)
    versioned = out_dir / f"{sha}.json"
    versioned.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    latest = out_dir / "latest.json"
    latest.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return latest


def load_bundle() -> dict:
    """读 latest.json;不存在则即时 build 一份。"""
    s = get_settings()
    latest = s.helper_data_dir / "bundles" / "latest.json"
    if not latest.exists():
        build_bundle()
    return json.loads(latest.read_text(encoding="utf-8"))
