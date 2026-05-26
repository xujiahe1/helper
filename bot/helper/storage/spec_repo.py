"""Spec git repo — 决策规约的真实落地,所有变更走 git 历史。

布局见 docs/architecture.md §9。
策略 yaml 放在 meta/policies/,bot 启动时若不存在则用打包默认值 seed。
"""

from __future__ import annotations

from pathlib import Path

from git import Repo

from helper.policy.loader import all_default_filenames, default_policy_text

# Spec repo 内的目录骨架(都放 .gitkeep)
SUBDIRS = [
    "ontology/entities",
    "ontology/relationships",
    "specs",
    "facts",
    "cases",
    "meta/policies",
]

_README = """\
# Helper Spec Repository

专家决策规约的真实落地。任何变更走 PR / git diff,可审计、可回溯。

## 目录

- `ontology/entities/` — 已晋升的核心 entity MD(决策性概念)
- `ontology/relationships/` — entity 间关系
- `specs/` — 决策规约
- `facts/` — 决策性事实
- `cases/` — 反例 / 验证用例
- `meta/policies/` — 知识化策略本身(详见上层 docs/architecture.md §8.6)

## 注意

- 在外部系统已有权威表的实体(员工 / 工单类型 / 系统名 / 项目)**不要**建在这里,
  只在 raw input 中 inline 即可,详细信息走外部系统 API 即时查。
- 晋升机制 / decay / 合并阈值由 `meta/policies/knowledge_policy.yaml` 驱动,
  改策略走 PR,改完跑 `helper policy evaluate --dry-run` 看影响面再 apply。
"""

_GITIGNORE = """\
.DS_Store
*.swp
"""


def init_spec_repo(spec_dir: Path) -> Repo:
    """首次启动时初始化 spec 仓库;重复调用幂等。

    返回 GitPython Repo 对象。
    """
    spec_dir = spec_dir.expanduser().resolve()
    spec_dir.mkdir(parents=True, exist_ok=True)

    is_new = not (spec_dir / ".git").exists()

    # 骨架目录 + .gitkeep
    for sub in SUBDIRS:
        d = spec_dir / sub
        d.mkdir(parents=True, exist_ok=True)
        keep = d / ".gitkeep"
        if not keep.exists():
            keep.touch()

    # README + .gitignore
    readme = spec_dir / "README.md"
    if not readme.exists():
        readme.write_text(_README, encoding="utf-8")
    gi = spec_dir / ".gitignore"
    if not gi.exists():
        gi.write_text(_GITIGNORE, encoding="utf-8")

    # Seed 默认策略(后续策略变更走 PR 改这些文件)
    policies_dir = spec_dir / "meta" / "policies"
    policies_dir.mkdir(parents=True, exist_ok=True)
    for name in all_default_filenames():
        target = policies_dir / name
        if not target.exists():
            target.write_text(default_policy_text(name), encoding="utf-8")

    if is_new:
        repo = Repo.init(spec_dir)
        repo.index.add(["."])
        repo.index.commit("init: helper spec repo skeleton + default policy")
        return repo

    repo = Repo(spec_dir)
    # 老 repo 若新增了 seed 文件,补一次 commit
    if repo.is_dirty(untracked_files=True):
        repo.git.add(A=True)
        repo.index.commit("seed: refresh skeleton + default policy")
    return repo
