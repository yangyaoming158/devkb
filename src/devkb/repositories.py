"""数据访问层（D7 数据隔离纪律）：

- 本文件是 src/devkb 下唯一允许构造 sqlalchemy 查询的位置（tests/unit/test_layering.py 静态强制）；
- 除 ProjectRepo（管理项目实体本身）外，Repository 以 project_id 实例化，
  查询方法不接受外部传入的 project_id——跨项目访问在 API 层面即不可表达。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import Select, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import ClauseElement, Executable

from devkb.fts import build_search_text, tokenize_query
from devkb.models import AgentRun, AgentStep, Chunk, Document, Project, ToolInvocation

VectorSearchMode = Literal["exact", "hnsw"]

# T15.3 之前 ef_search 未冻结；上限防误配（pgvector 合法范围 1–1000）
MAX_EF_SEARCH = 1000


class _Explain(Executable, ClauseElement):
    """EXPLAIN (ANALYZE, BUFFERS) 包装：内层语句连同绑定参数原样执行，无字符串拼接。"""

    inherit_cache = False

    def __init__(self, statement: Select[Any]) -> None:
        self.statement = statement


@compiles(_Explain)
def _compile_explain(element: _Explain, compiler: Any, **kw: Any) -> str:
    return "EXPLAIN (ANALYZE, BUFFERS) " + compiler.process(element.statement, **kw)


class ProjectRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, slug: str, name: str) -> Project:
        project = Project(slug=slug, name=name)
        self._session.add(project)
        await self._session.flush()
        return project

    async def get_by_slug(self, slug: str) -> Project | None:
        return (
            await self._session.execute(select(Project).where(Project.slug == slug))
        ).scalar_one_or_none()


@dataclass(frozen=True)
class ChunkDraft:
    """摄取管道产出的待入库块（无身份，身份由仓储赋予）。"""

    ordinal: int
    title_path: str
    content: str
    content_hash: str
    token_count: int
    start_line: int
    end_line: int
    embedding: list[float] | None = None


class DocumentRepo:
    def __init__(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        self._session = session
        self._project_id = project_id

    async def get_by_rel_path(self, rel_path: str) -> Document | None:
        stmt = select(Document).where(
            Document.project_id == self._project_id, Document.rel_path == rel_path
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def upsert(
        self, *, rel_path: str, title: str, doc_type: str, content_hash: str
    ) -> Document:
        stmt = (
            pg_insert(Document)
            .values(
                id=uuid.uuid4(),
                project_id=self._project_id,
                rel_path=rel_path,
                title=title,
                doc_type=doc_type,
                content_hash=content_hash,
                status="active",
                parse_error=None,
            )
            .on_conflict_do_update(
                index_elements=[Document.project_id, Document.rel_path],
                set_={
                    "title": title,
                    "doc_type": doc_type,
                    "content_hash": content_hash,
                    "status": "active",
                    "parse_error": None,
                    "updated_at": func.now(),
                },
            )
            .returning(Document)
        )
        # populate_existing：同一行已在身份映射中时，用 RETURNING 的新值刷新旧对象
        # （否则二次 upsert 会拿到第一次的陈旧属性——集成测试抓到的真实陷阱）
        result = await self._session.execute(stmt, execution_options={"populate_existing": True})
        return result.scalar_one()

    async def mark_failed(self, rel_path: str, error: str, *, doc_type: str = "markdown") -> None:
        stmt = (
            pg_insert(Document)
            .values(
                id=uuid.uuid4(),
                project_id=self._project_id,
                rel_path=rel_path,
                title="",
                doc_type=doc_type,
                content_hash="",
                status="failed",
                parse_error=error[:2000],
            )
            .on_conflict_do_update(
                index_elements=[Document.project_id, Document.rel_path],
                set_={"status": "failed", "parse_error": error[:2000], "updated_at": func.now()},
            )
        )
        await self._session.execute(stmt)

    async def list_active(self) -> Sequence[Document]:
        stmt = (
            select(Document)
            .where(Document.project_id == self._project_id, Document.status == "active")
            .order_by(Document.rel_path)
        )
        return (await self._session.execute(stmt)).scalars().all()


class ChunkRepo:
    def __init__(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        self._session = session
        self._project_id = project_id

    async def replace_for_document(
        self, document_id: uuid.UUID, drafts: Sequence[ChunkDraft]
    ) -> int:
        """单文档事务内删旧插新（规格 §7）。

        search_text 在此统一计算（T12.2 复评修复）：chunk 的唯一写入口径收口后，
        新摄取/重摄取与 backfill 必然使用同一 token 化函数，不存在"忘了填"的路径。
        """
        await self._session.execute(
            delete(Chunk).where(
                Chunk.project_id == self._project_id, Chunk.document_id == document_id
            )
        )
        self._session.add_all(
            Chunk(
                project_id=self._project_id,
                document_id=document_id,
                ordinal=d.ordinal,
                title_path=d.title_path,
                content=d.content,
                content_hash=d.content_hash,
                token_count=d.token_count,
                start_line=d.start_line,
                end_line=d.end_line,
                embedding=d.embedding,
                search_text=build_search_text(d.title_path, d.content),
            )
            for d in drafts
        )
        await self._session.flush()
        return len(drafts)

    async def vector_search(
        self,
        embedding: list[float],
        top_k: int,
        *,
        mode: VectorSearchMode = "exact",
        ef_search: int | None = None,
    ) -> list[tuple[Chunk, str, float]]:
        """余弦相似度检索，返回 (chunk, 所属文档 rel_path, 相似度) 降序。

        - exact：关 indexscan——评测基线要求真精确，不能让 planner 静默用
          HNSW 近邻序（btree/顺扫怎么选都不影响精确性）；
        - hnsw：关 seqscan 与 sort——距离序只能由 0002 的 HNSW 索引提供；
          只关 seqscan 不够，小表下 planner 会走 btree+Sort 得出"假 HNSW"的
          精确结果，让 T15.3 的 overlap 对照失去意义。
        所有 GUC（含 hnsw.ef_search）先记录调用前值、查询后恢复原值（T12.3 复评
        修复：不能写死恢复 'on'，结果不得依赖同事务内的调用顺序）。查询抛错时
        事务必然中止，set_config(is_local) 随回滚一并撤销，无需 try/finally。

        current_setting 必须带 missing_ok（T18.2 真实 run 实测）：hnsw.ef_search
        是 pgvector 扩展 GUC，连接内 vector 库尚未加载（首个查询即 hnsw）时读取
        直接抛 UndefinedObject 并中止事务；missing_ok 返回 NULL，此时 set_config
        先建占位符，索引扫描加载库后生效（占位值与 pgvector 默认同为 40），
        无调用前值的 GUC 不恢复（is_local 使其最迟事务结束即失效）。

        仅检索 status='active' 文档的 chunks：active 文档更新失败时事务回滚会保留
        上一版 chunks（文档已标 failed），不过滤会引用与当前文件行号不符的陈旧内容。
        """
        overrides: dict[str, str] = (
            {"enable_indexscan": "off"}
            if mode == "exact"
            else {"enable_seqscan": "off", "enable_sort": "off"}
        )
        if mode == "hnsw" and ef_search is not None:
            if not 1 <= ef_search <= MAX_EF_SEARCH:
                raise ValueError(f"ef_search 必须在 1..{MAX_EF_SEARCH}：{ef_search}")
            overrides["hnsw.ef_search"] = str(ef_search)

        names = list(overrides)
        previous_row = (
            await self._session.execute(select(*[func.current_setting(n, True) for n in names]))
        ).one()
        previous = {
            name: value
            for name, value in zip(names, previous_row, strict=True)
            if value is not None
        }
        for name, value in overrides.items():
            await self._session.execute(select(func.set_config(name, value, True)))

        distance = Chunk.embedding.cosine_distance(embedding).label("distance")
        stmt = (
            select(Chunk, Document.rel_path, distance)
            .join(Document, Chunk.document_id == Document.id)
            .where(
                Chunk.project_id == self._project_id,
                Chunk.embedding.is_not(None),
                Document.status == "active",
            )
            .order_by(distance)
            .limit(top_k)
        )
        rows = (await self._session.execute(stmt)).all()
        for name, value in previous.items():
            await self._session.execute(select(func.set_config(name, value, True)))
        return [(row[0], row[1], 1.0 - float(row[2])) for row in rows]

    async def lexical_search(self, query: str, top_k: int) -> list[tuple[Chunk, str, float]]:
        """FTS 检索（§8 lexical channel），返回 (chunk, rel_path, ts_rank_cd 分数) 降序。

        查询构造（T14.2 冻结）：原始 query 经 tokenize_query（与摄取期同一冻结
        函数，两侧词素必然对称）取 ≤32 个去重 token，每个 token 作为**绑定参数**
        经 plainto_tsquery('simple', ...) 生成子查询，再用 tsquery `||`（OR）合并：

        - OR 而非 websearch 的 AND：中英混合问题（"OrderStateMachine 在什么情况
          下…"）在 AND 语义下几乎必然零命中，会杀死整个 lexical channel；OR 之下
          ts_rank_cd 的 cover density 自然让多词命中排前；
        - token 内部保持 AND：'simple' 解析器把 order_status 拆成 order & status，
          与索引侧 to_tsvector 对同一 token 的拆法完全一致；
        - plainto_tsquery 对任意输入都不抛语法错误，且全程参数绑定——特殊字符
          只可能不命中，不可能注入或崩溃。

        排序用 (rank desc, chunk_id) 保证并列时输出确定。
        """
        stmt = self._lexical_stmt(query, top_k)
        if stmt is None:
            return []
        rows = (await self._session.execute(stmt)).all()
        return [(row[0], row[1], float(row[2])) for row in rows]

    def _lexical_stmt(
        self,
        query: str,
        top_k: int,
        *,
        rank_normalization: int = 0,
        drop_single_cjk: bool = False,
    ) -> Select[tuple[Chunk, str, Any]] | None:
        """lexical 查询构造的唯一出处；两个关键字参数仅供诊断路径使用。

        rank_normalization=0 时用二参 ts_rank_cd（与 T14.2 冻结的业务语句完全
        一致，不产生任何 SQL 差异）；非 0 时以第三参传入 PG 的归一化 flag。
        """
        tokens = tokenize_query(query)
        if drop_single_cjk:
            tokens = [t for t in tokens if not (len(t) == 1 and not t.isascii())]
        if not tokens:
            return None
        parts = [func.plainto_tsquery("simple", token) for token in tokens]
        tsquery = parts[0]
        for part in parts[1:]:
            tsquery = tsquery.op("||")(part)
        if rank_normalization:
            rank = func.ts_rank_cd(Chunk.search_tsv, tsquery, rank_normalization).label("rank")
        else:
            rank = func.ts_rank_cd(Chunk.search_tsv, tsquery).label("rank")
        return (
            select(Chunk, Document.rel_path, rank)
            .join(Document, Chunk.document_id == Document.id)
            .where(
                Chunk.project_id == self._project_id,
                Document.status == "active",
                Chunk.search_tsv.op("@@")(tsquery),
            )
            .order_by(rank.desc(), Chunk.id)
            .limit(top_k)
        )

    async def lexical_search_diagnostic(
        self,
        query: str,
        top_k: int,
        *,
        rank_normalization: int = 0,
        drop_single_cjk: bool = False,
    ) -> list[tuple[Chunk, str, float]]:
        """诊断专用（T14.4 复评归档）：与 lexical_search 共用同一查询构造，
        仅开放 ts_rank_cd 归一化 flag 与"丢弃单字 CJK token"两个实验参数。

        默认参数下与 lexical_search 结果逐行一致（集成测试锁定），从而保证
        排序变体对照实验"只改变排名参数、其余口径相同"。不在业务路径调用。
        """
        stmt = self._lexical_stmt(
            query,
            top_k,
            rank_normalization=rank_normalization,
            drop_single_cjk=drop_single_cjk,
        )
        if stmt is None:
            return []
        rows = (await self._session.execute(stmt)).all()
        return [(row[0], row[1], float(row[2])) for row in rows]

    async def explain_lexical_search(self, query: str, top_k: int) -> str:
        """对与 lexical_search 完全同源的查询形状跑 EXPLAIN (ANALYZE, BUFFERS)。

        评测报告用（T14.4：真实语料上验证 GIN 计划），不在业务路径调用。
        经 _Explain 包装编译：内层语句与全部绑定参数原样保留（extended protocol
        下 EXPLAIN 可带参执行），与 lexical_search 一样不做任何字符串拼接
        （T14.2 复评修复：撤销 literal_binds 内联）。
        """
        stmt = self._lexical_stmt(query, top_k)
        if stmt is None:
            return ""
        result = await self._session.execute(_Explain(stmt))
        return "\n".join(result.scalars())

    async def count(self) -> int:
        stmt = select(func.count()).where(Chunk.project_id == self._project_id)
        return (await self._session.execute(stmt)).scalar_one()

    async def backfill_search_text(self) -> tuple[int, int]:
        """用与新摄取相同的 build_search_text 重算当前项目全部 chunks 的 search_text。

        面向迁移前存量数据和 token 化函数升级（T14 冻结时若有调整）两种场景。
        返回 (总数, 实际更新数)；结果已一致的行跳过，重复执行幂等。
        只改 search_text 单列（search_tsv 为生成列自动跟随），不触碰 embedding。
        事务由调用方掌控：不在此 commit，失败时整体可回滚（T12.2）。
        """
        rows = (
            await self._session.execute(
                select(Chunk.id, Chunk.title_path, Chunk.content, Chunk.search_text)
                .where(Chunk.project_id == self._project_id)
                .order_by(Chunk.id)
            )
        ).all()
        updated = 0
        for chunk_id, title_path, content, current in rows:
            desired = build_search_text(title_path, content)
            if desired == current:
                continue
            await self._session.execute(
                update(Chunk)
                .where(Chunk.project_id == self._project_id, Chunk.id == chunk_id)
                .values(search_text=desired)
            )
            updated += 1
        await self._session.flush()
        return len(rows), updated

    async def list_reference_anchors(self) -> list[tuple[str, str]]:
        """列出当前项目可检索 chunk 的路径和标题路径。

        评测运行前用它验证 rel_path + anchor 标注在当前语料快照中
        可解析，避免把评测天花板缺失误判为检索器质量问题。
        """
        stmt = (
            select(Document.rel_path, Chunk.title_path)
            .join(Document, Chunk.document_id == Document.id)
            .where(
                Chunk.project_id == self._project_id,
                Document.status == "active",
            )
            .order_by(Document.rel_path, Chunk.ordinal)
        )
        rows = (await self._session.execute(stmt)).all()
        return [(row[0], row[1]) for row in rows]


class RunRepo:
    def __init__(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        self._session = session
        self._project_id = project_id

    async def create(self, question: str) -> AgentRun:
        run = AgentRun(project_id=self._project_id, question=question, status="running")
        self._session.add(run)
        await self._session.flush()
        return run

    async def finish(
        self,
        run_id: uuid.UUID,
        *,
        answer: dict[str, Any],
        model: str,
        tokens_in: int,
        tokens_out: int,
        usage: dict[str, Any] | None,
        cost: Decimal | None,
        latency_ms: int,
        status: str = "succeeded",
    ) -> None:
        run = await self.get(run_id)
        if run is None:
            return
        run.answer = answer
        run.model = model
        run.tokens_in = tokens_in
        run.tokens_out = tokens_out
        run.usage = usage
        run.cost = cost
        run.latency_ms = latency_ms
        run.status = status
        await self._session.flush()

    async def get(self, run_id: uuid.UUID) -> AgentRun | None:
        stmt = select(AgentRun).where(
            AgentRun.project_id == self._project_id, AgentRun.id == run_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_recent(self, limit: int = 20) -> Sequence[AgentRun]:
        stmt = (
            select(AgentRun)
            .where(AgentRun.project_id == self._project_id)
            .order_by(AgentRun.created_at.desc())
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()


class AgentStepRepo:
    """节点轨迹写读（§5.2/§11）。summary 字段由调用方负责脱敏与限长。"""

    def __init__(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        self._session = session
        self._project_id = project_id

    async def add(
        self,
        run_id: uuid.UUID,
        *,
        seq: int,
        node: str,
        status: str,
        attempt: int = 1,
        input_summary: dict[str, Any] | None = None,
        output_summary: dict[str, Any] | None = None,
        latency_ms: int | None = None,
        error: str | None = None,
    ) -> AgentStep:
        step = AgentStep(
            project_id=self._project_id,
            run_id=run_id,
            seq=seq,
            node=node,
            attempt=attempt,
            status=status,
            input_summary=input_summary,
            output_summary=output_summary,
            latency_ms=latency_ms,
            error=error,
        )
        self._session.add(step)
        await self._session.flush()
        return step

    async def list_for_run(self, run_id: uuid.UUID) -> Sequence[AgentStep]:
        """按 seq 升序返回本项目该 run 的全部节点轨迹；跨项目 run_id 得到空列表。"""
        stmt = (
            select(AgentStep)
            .where(AgentStep.project_id == self._project_id, AgentStep.run_id == run_id)
            .order_by(AgentStep.seq)
        )
        return (await self._session.execute(stmt)).scalars().all()


class ToolInvocationRepo:
    """工具轨迹写读（§5.3/§11）。arguments/result_summary 由调用方负责脱敏与限长。"""

    def __init__(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        self._session = session
        self._project_id = project_id

    async def add(
        self,
        run_id: uuid.UUID,
        step_id: uuid.UUID,
        *,
        tool_name: str,
        status: str,
        arguments: dict[str, Any] | None = None,
        result_summary: dict[str, Any] | None = None,
        latency_ms: int | None = None,
        error: str | None = None,
    ) -> ToolInvocation:
        invocation = ToolInvocation(
            project_id=self._project_id,
            run_id=run_id,
            step_id=step_id,
            tool_name=tool_name,
            status=status,
            arguments=arguments,
            result_summary=result_summary,
            latency_ms=latency_ms,
            error=error,
        )
        self._session.add(invocation)
        await self._session.flush()
        return invocation

    async def list_for_run(self, run_id: uuid.UUID) -> Sequence[ToolInvocation]:
        """按所属 step 的 seq 升序返回；跨项目 run_id 得到空列表。

        同一 step 内多次调用以 (created_at, id) 兜底排序——created_at 是事务时间戳，
        同事务内并列，严格的步内顺序语义待 T18 落轨迹时按需明确。
        """
        stmt = (
            select(ToolInvocation)
            .join(AgentStep, ToolInvocation.step_id == AgentStep.id)
            .where(
                ToolInvocation.project_id == self._project_id,
                ToolInvocation.run_id == run_id,
            )
            .order_by(AgentStep.seq, ToolInvocation.created_at, ToolInvocation.id)
        )
        return (await self._session.execute(stmt)).scalars().all()


async def get_alembic_version(session: AsyncSession) -> str | None:
    """healthz 探测用：当前迁移版本（alembic_version 单行表，无版本时 None）。"""
    result = await session.execute(text("SELECT version_num FROM alembic_version"))
    return result.scalar_one_or_none()
