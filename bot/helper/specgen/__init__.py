"""L2 — N 条 L1 → candidate specs。

改动 3 后, 主链路换成 SpecTopic 语义聚类:
1. sink 写完 L1Item 后异步 assign_topic_for_raw 把 decision 归簇
2. 周期性 dispatcher 跑 scan_topics_for_draft 找该触发的 topic
3. draft_spec_from_topic 把 topic 转 cluster_keys 后跑 LLM 出 spec

旧 cluster_l1_results / draft_spec_from_cluster 入口保留, server.py / cli.py
的 `/specgen run` 仍可用 — 内部走的是改动 3 后的兼容层 (扫 topic 出簇)。
"""

from helper.specgen.cluster import (
    assign_topic,
    assign_topic_for_raw,
    cluster_l1_results,
    scan_topics_for_draft,
    topic_keys,
)
from helper.specgen.draft import (
    draft_spec_from_cluster,
    draft_spec_from_topic,
    promote_spec,
)

__all__ = [
    "assign_topic",
    "assign_topic_for_raw",
    "cluster_l1_results",
    "draft_spec_from_cluster",
    "draft_spec_from_topic",
    "promote_spec",
    "scan_topics_for_draft",
    "topic_keys",
]
