"""L2 — N 条 L1 → candidate specs。

聚类 + LLM draft → spec_candidates 表 → review → promote 到 git specs/<slug>.md。
"""

from helper.specgen.cluster import cluster_l1_results
from helper.specgen.draft import draft_spec_from_cluster, promote_spec

__all__ = ["cluster_l1_results", "draft_spec_from_cluster", "promote_spec"]
