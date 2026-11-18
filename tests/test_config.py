"""Settings(설정)의 기본값이 스펙과 같은지, 비밀번호 원문이 아니라 해시만 받는지 검증한다."""

import pytest
from pydantic import ValidationError

from app.config import Settings
from tests.conftest import settings_factory


# 기본값이 스펙(6절)·계획서 Global Constraints와 같은지 확인한다.
def test_defaults_match_spec():
    s = settings_factory()
    assert (s.embedding_model, s.embedding_dim) == ("gemini-embedding-2", 768)
    assert (s.test_ip_limit, s.rerank_monthly_cap, s.max_llm_calls) == (5, 950, 8)
    assert (s.max_regen, s.max_research, s.max_question_chars) == (2, 2, 1000)
    assert (s.search_depth, s.rerank_min_score) == (150, 0.0)


# 비밀번호 원문 필드는 없고, 필수 값이 없으면 설정 생성이 실패하는지 확인한다.
def test_requires_hashes_not_plaintext_passwords():
    assert "admin_password" not in Settings.model_fields
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # 필수 값 없음


# OpenSearch는 보안 기능을 켠 채로 쓴다(사용자 결정 2026-11-18): https 주소, 계정 admin, 비밀번호는 필수(기본값 없음).
def test_opensearch_defaults_expect_security_on():
    s = settings_factory()
    assert (s.opensearch_url, s.opensearch_user) == ("https://127.0.0.1:9200", "admin")
    assert Settings.model_fields["opensearch_password"].is_required()


# 환경 변수는 LABOR_ 접두어로 읽혀야 한다(서버 .env가 쓰는 방식).
def test_env_variables_use_labor_prefix(monkeypatch):
    monkeypatch.setenv("LABOR_MAX_LLM_CALLS", "3")
    assert settings_factory().max_llm_calls == 3


# 대화 체크포인트는 앱 DB(data/app.db)와 다른 파일에 둔다. 정리 스크립트(prune_checkpoints.py)의 기본 대상과 같은 경로다.
def test_checkpoint_file_is_separate_from_the_app_db():
    s = settings_factory()
    assert s.checkpoint_path == "data/checkpoints.db" and s.checkpoint_path != s.sqlite_path
