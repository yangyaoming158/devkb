from pathlib import Path

import pytest

from devkb.config import get_settings
from devkb.errors import ConfigError


def test_missing_required_key_raises_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # env_file=".env" 是相对路径，chdir 到空目录即隔离项目根的真实 .env
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEVKB_LLM_API_KEY", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ConfigError, match="DEVKB_LLM_API_KEY"):
        get_settings()
    get_settings.cache_clear()


def test_defaults_follow_adr_0002(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEVKB_LLM_API_KEY", "sk-test")
    get_settings.cache_clear()
    s = get_settings()
    assert s.embedding_model_id == "Qwen/Qwen3-Embedding-0.6B"
    assert s.embedding_batch_size <= 32  # ADR-0002：batch>32 有显存悬崖
    assert s.llm_base_url == "https://api.deepseek.com"
    assert s.llm_api_key.get_secret_value() == "sk-test"
    assert str(s.llm_api_key) == "**********"  # SecretStr 防止误打日志
    get_settings.cache_clear()
