-- 仅在数据卷首次初始化时执行（docker-entrypoint-initdb.d 机制）
CREATE EXTENSION IF NOT EXISTS vector;
