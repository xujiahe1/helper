-- M11: 完全砍掉 v1 L1 候选表(fact / case / concept / relation)
-- 一次性手动执行,本地 + 服务器各跑一次。
--
-- 前置:确认 v1 数据已重跑成 v2 section/decision(已确认)。
-- 影响:
--   * 4 张 v1 候选表 + 索引 row drop
--   * fts_items / vec_items 里残留的 entity/fact/case/relation kind 行删除
--   * conflict_log 里 target_type ∈ (fact/case/concept/relation) 的行(如有)
--     标 auto_rejected 关闭(这次审计实测库里只有 1 条 spec target,无 v1 target)
--
-- 执行:
--   sqlite3 var/helper/helper.db < scripts/drop_v1_tables.sql
--
-- 服务器:
--   ssh root@10.234.81.212
--   sqlite3 /var/lib/helper/helper.db < /tmp/drop_v1_tables.sql

BEGIN TRANSACTION;

-- 1. 关闭尚 open 的 v1 target 冲突(防御性,实测应为 0)
UPDATE conflict_log
SET resolution = 'auto_rejected',
    resolved_by = 'm11-v1-drop',
    resolved_at = strftime('%Y-%m-%d %H:%M:%f', 'now'),
    auto_reason = 'v1 已废弃(M11 退役)'
WHERE resolution = 'open'
  AND target_type IN ('fact', 'case', 'concept', 'relation');

-- 2. 清 fts 索引中 v1 kind 残留(vec_items 走 sqlite-vec 模块,需在 Python 运行时清,
--    见步骤 4)
DELETE FROM fts_items WHERE kind IN ('entity', 'fact', 'case', 'relation');

-- 3. drop 4 张 v1 候选表
DROP TABLE IF EXISTS entity_candidates;
DROP TABLE IF EXISTS fact_candidates;
DROP TABLE IF EXISTS case_candidates;
DROP TABLE IF EXISTS relation_candidates;

COMMIT;

-- 验证(执行完手动检查):
-- .tables                  -- 4 张表应消失
-- SELECT COUNT(*) FROM fts_items WHERE kind IN ('entity','fact','case','relation');  -- 应为 0

-- 4. vec_items 残留清理 — 在 Python 运行时跑(sqlite-vec 模块需要加载):
--    cd bot && python -c "
--    from helper.config import get_settings
--    from helper.storage import init_engine, session
--    from helper.storage.vector import delete_kind
--    s = get_settings()
--    init_engine(s.helper_data_dir / 'helper.db')
--    with session() as sess:
--        for k in ('entity', 'fact', 'case', 'relation'):
--            delete_kind(sess, kind=k)
--        sess.commit()
--    "
--    delete_kind 当前不存在 — 见 helper/storage/vector.py 已有 clear_all,
--    可改用 'reindex --clear' CLI 一次清干净再重建 spec/section/decision。
