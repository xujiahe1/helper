"""文档批量 ingest — 整篇长文档切片 → bulk_extract 抽决策候选 → 入 raw_inputs。

约定:
  - 输入支持 .md / .txt / .json(KM 文档导出可走 dump JSON)
  - 切片走规则(段落 + 长度上限),不用 LLM 切
  - 每片都先抽"是否含决策" → 含则作为一条 raw 入库,跑完自动 schedule_l1
  - 严格串行,服务器只 800M cgroup,不能并发(见 docs/runtime.md §3)
"""

from helper.batch.ingest import ingest_file, ingest_text_units, split_into_units

__all__ = ["ingest_file", "ingest_text_units", "split_into_units"]
