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
    # 0.15 = Task 9 Step 7 측정(docs/step0-report.md 8절)의 일상어형 후보를 사용자가 정했다(2026-11-18). 임시값이 아니다.
    # 깊이 100은 Task 9 Step 7 측정에서 가장 좋았고(일상어형 Recall@20 0.94 vs 0.86·0.90) 값이 작아 가볍다. 우연일 수 있어
    # Task 17 전체 평가에서 다시 확인한다.
    assert (s.search_depth, s.rerank_min_score) == (100, 0.15)


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


# 법제처 Open API는 v1에서 쓰지 않으므로(스펙 개정 1) 서버 .env에 LABOR_LAW_API_OC가 없어도 서버가 시작된다.
# 나머지 비밀 값(키·해시·세션 비밀)은 빠지면 시작이 실패해야 한다 — 이 시험이 "필수 해제"가 번지지 않게 막는다.
def test_law_api_oc_is_optional_but_every_other_secret_stays_required():
    assert not Settings.model_fields["law_api_oc"].is_required()
    secrets = {
        "gemini_api_key", "cohere_api_key", "opensearch_password", "admin_password_hash", "test_password_hash",
        "session_secret", "langfuse_public_key", "langfuse_secret_key", "langfuse_host",
    }
    assert all(Settings.model_fields[name].is_required() for name in secrets)
    values = {name: "x" for name in secrets}
    assert Settings(_env_file=None, **values).law_api_oc == ""  # 값이 없을 때의 기본은 빈 문자열


# 환경 변수는 LABOR_ 접두어로 읽혀야 한다(서버 .env가 쓰는 방식).
def test_env_variables_use_labor_prefix(monkeypatch):
    monkeypatch.setenv("LABOR_MAX_LLM_CALLS", "3")
    assert settings_factory().max_llm_calls == 3


# 대화 체크포인트는 앱 DB(data/app.db)와 다른 파일에 둔다. 정리 스크립트(prune_checkpoints.py)의 기본 대상과 같은 경로다.
def test_checkpoint_file_is_separate_from_the_app_db():
    s = settings_factory()
    assert s.checkpoint_path == "data/checkpoints.db" and s.checkpoint_path != s.sqlite_path


# Task 17 모델 비교(5개 후보, 일상어 질문 15건)에서 합격선을 넘은 가장 작은 모델을 기본값으로 쓴다(docs/step0-report.md 10절의 표).
def test_default_llm_model_is_the_task17_choice():
    assert settings_factory().llm_model == "gemini-3.1-flash-lite"
