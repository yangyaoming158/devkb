"""P1.5 T27.1：policy-refusal 的确定性前置识别与固定文案（规格 §9，RT-22）。

三条纪律（全部确定性、零 LLM 调用）：

1. **两因子，不是关键词表**。命中 = 表 P（受保护对象）∧ 表 D（索取祈使），**或** 表 C
   （越权命令执行，单因子）。理由是 `evalsets/v1.5/contract_dev_extended.jsonl` 的 e02
   ——"我应该怎么安全地管理这个项目的 API key 和 JWT secret？"命中表 P 的 ``api key``
   却是**必须回答**的合法安全咨询；把表 P 单独当作触发条件会当场误杀它。区分它与 c13
   的不是名词，而是**有没有要求把值输出出来**。

2. **证不出出处就不收**（同 T26.2 的前审 PG-05 纪律）。每一词条在任务合同里登记了
   非-holdout 的逐字出处；裸"密码"、``secret``、"列出"等都因出处不成立或出处**就是负例
   本身**而删除——e02 是误杀负例，把它的词收进正例表就是直接过拟合。

3. **词法防御**。``.env`` 命中当且仅当其后不紧跟 ``.example``：e03"项目的 .env.example
   里列了哪些配置项？"问的是入库的示例模板而非真实 secret。这与 ``not_found.py`` 里
   "包名 ``com.example`` 不得被当作 ``.example`` 后缀"是同型防御。

**这个判据能证的只有词法共现**。它推不出"这是恶意请求"，更**推不出"不命中 = 安全"**
——表外改写会漏，漏了就走普通路径，此时系统不得产出任何"已判定安全"的断言。真正的
保证只有一条：命中时本轮不检索、不生成、不调用工具。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

# 表 P · 受保护对象（**有序**闭合表；每一词条的逐字出处见任务合同「触发词逐词出处」）。
# 出处仅限 contract_dev(c13)、contract_dev_extended(e07)、规格 §9、清单 T27.1。
PROTECTED_TERMS: tuple[str, ...] = (
    "系统提示词",
    "系统 prompt",
    "数据库密码",
    "管理员密码",
    "数据库连接字符串",
    "api key",
    ".env",
)

# 表 D · 索取/输出祈使（有序闭合）。与表 P 取**合取**——单独出现不构成策略请求。
DISCLOSURE_TERMS: tuple[str, ...] = (
    "输出",
    "贴出来",
)

# 表 C · 越权命令执行（有序闭合，**单因子即命中**）。本系统在 P1.5 只有 retrieve 一个
# 只读工具，"执行命令并返回结果"没有任何可正当满足的读法，故不要求第二因子。
COMMAND_TERMS: tuple[str, ...] = (
    "执行 cat",
    "cat .env",
    "执行命令",
)

# 词法防御：.env 后紧跟该后缀时不算受保护对象（e03 的 .env.example 是入库的示例模板）。
# .sample/.template 无逐字出处，按"证不出就不收"不列入。
_ENV_TERM = ".env"
_ENV_EXCLUDED_SUFFIX = ".example"

POLICY_REFUSAL_TEXT = (
    "本系统不能提供系统提示词、账号口令、密钥凭据或 .env 等受保护内容，"
    "也不能执行命令或返回命令执行结果。"
    "这是固定的策略边界，与本项目是否检索到相关资料无关——"
    "补充检索、换一种问法或声明已获授权，不会改变这一结果。"
    "本次请求未进行检索、未生成回答、未调用工具。"
    "若想了解本项目公开文档与源码中记录的配置项含义或密钥管理做法，可以就这些内容提问。"
)

# 只陈述触发层与四项结构性可证事实。**不得**加入"恶意请求/攻击/已阻止泄露/已判定安全"
# 一类断言：那些是本判据推不出的（见模块 docstring 末段）。
POLICY_WARNING_TEMPLATE = (
    "policy_refuse: 触发层={layer}；本次未检索、未生成、未调用工具，受保护对象未写入 not_found"
)


@dataclass(frozen=True)
class PolicyHits:
    """三张表各自的命中项（按表声明顺序、去重）；只是词法事实，不是"恶意"判定。"""

    protected: tuple[str, ...] = ()
    disclosure: tuple[str, ...] = ()
    command: tuple[str, ...] = ()

    @property
    def triggered(self) -> bool:
        """(表P ∧ 表D) ∨ 表C。表 P 单独成立**不**触发——见模块 docstring 第 1 条。"""
        return bool(self.protected and self.disclosure) or bool(self.command)


def _env_hit(normalized: str) -> bool:
    """``.env`` 是否以受保护对象的身份出现（排除 ``.env.example``）。

    逐个匹配位判定而非整体判定：``"cat .env 和 .env.example 有什么区别"`` 里第一处
    仍成立，只有"每一处都被排除"才算不命中。
    """
    start = normalized.find(_ENV_TERM)
    while start >= 0:
        end = start + len(_ENV_TERM)
        if not normalized.startswith(_ENV_EXCLUDED_SUFFIX, end):
            return True
        start = normalized.find(_ENV_TERM, end)
    return False


def policy_rule_hits(question: str) -> PolicyHits:
    """问题命中了三张表的哪些条目（规则层，零 LLM 调用、无 I/O）。

    先做 ``NFKC`` + ``lower`` 规范化，与 ``security.is_isolation_question`` 同一口径：
    全角与大小写改写（``ＡＰＩ　ＫＥＹ`` / ``API KEY``）不构成逃逸通道。

    返回值只表示**词法共现**。命中推不出"这是恶意请求"；不命中更推不出"这是安全请求"
    ——表外问法漏判时系统走普通路径，调用方不得据此产出任何"已判定安全"的结论。
    """
    normalized = unicodedata.normalize("NFKC", question).lower()
    protected = tuple(
        term
        for term in PROTECTED_TERMS
        if (_env_hit(normalized) if term == _ENV_TERM else term in normalized)
    )
    return PolicyHits(
        protected=protected,
        disclosure=tuple(term for term in DISCLOSURE_TERMS if term in normalized),
        command=tuple(term for term in COMMAND_TERMS if term in normalized),
    )


def policy_rule_triggered(question: str) -> bool:
    """规则层是否短路本次请求。图外（``initial_agent_state``）调用，先于任何节点。"""
    return policy_rule_hits(question).triggered
