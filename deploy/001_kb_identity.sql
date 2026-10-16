-- LitTrace 001：知识库集合身份与状态
-- 先备份数据库；执行期间停止应用写入。
-- 本脚本【不】推断旧集合归属，也【不】静默把旧库改名成新库。
BEGIN;
ALTER TABLE knowledge_bases ADD COLUMN IF NOT EXISTS vector_collection varchar(128);
ALTER TABLE knowledge_bases ADD COLUMN IF NOT EXISTS status varchar(16) NOT NULL DEFAULT 'ready';
COMMIT;
-- 旧库 vector_collection 为 NULL 是【有意保留】的：新代码对它返回 409 / requires_reindex。
SELECT id, user_id, name FROM knowledge_bases WHERE vector_collection IS NULL;
-- 恢复前先排查重复检查点：不做自动破坏性清理。
SELECT session_id, count(*) FROM research_checkpoints GROUP BY session_id HAVING count(*)>1;
