"""应用配置（pydantic-settings，环境变量前缀 DEVKB_，可选 .env）。

配置项清单与默认值以《P0实现规格》§9 为准；embedding 相关默认值来自 ADR-0002 实测约束。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from devkb.errors import ConfigError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DEVKB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["dev", "test"] = "dev"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://devkb:devkb-local@127.0.0.1:5432/devkb"

    # LLM（ADR-0004：DeepSeek，OpenAI 兼容端点）
    llm_api_key: SecretStr  # 必填，缺失时启动即报 ConfigError
    llm_base_url: str = "https://api.deepseek.com"
    # 显式模型名：deepseek-chat 别名 2026-07-24 弃用（ADR-0004，2026-07-13 官网核实）
    llm_model: str = "deepseek-v4-flash"

    # Embedding（ADR-0002 冻结：Qwen3-Embedding-0.6B / 1024 维 / fp16 / batch≤32）
    embedding_model_id: str = "Qwen/Qwen3-Embedding-0.6B"
    embedding_device: Literal["cuda", "cpu"] = "cuda"
    embedding_batch_size: int = 32  # ADR-0002：batch 64 实测显存溢出致吞吐悬崖，勿调大

    chunk_target_tokens: int = 400  # 分块目标 300–500 区间的中值
    # 上限须与 retrieval.MAX_FINAL_TOP_K 一致（config 不 import retrieval，
    # 由 tests/unit/test_config.py 断言防漂移）
    retrieval_top_k: int = Field(default=8, ge=1, le=12)


@lru_cache
def get_settings() -> Settings:
    """加载配置；必填项缺失时抛 ConfigError（而非裸 ValidationError）。"""
    try:
        # 必填字段由环境变量/.env 运行时注入，静态检查器不可见
        return Settings()  # pyright: ignore[reportCallIssue]
    except ValidationError as exc:
        missing = "、".join(
            f"DEVKB_{str(e['loc'][0]).upper()}" for e in exc.errors() if e["type"] == "missing"
        )
        raise ConfigError(f"配置缺失或非法：{missing or exc}") from exc
