"""서비스 조립(app/container.py)과, 인자 없이 부르는 `create_app()`을 시험한다 (Task 14).

조립 코드는 진짜 어댑터(Gemini·OpenSearch·Cohere)를 만들지만 **만들 때 네트워크를 쓰지 않아야** 한다. 그래서 이 파일의 시험은
소켓 연결을 막은 채로 돌려 그 약속을 증명한다.
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.adapters.cohere_rerank import CohereReranker
from app.adapters.gemini import GeminiEmbedder, GeminiLlm
from app.adapters.opensearch_store import OpenSearchStore
from app.adapters.sqlite_store import AppStore
from app.api.main import create_app
from app.container import build_deps, build_graph_factory, make_checkpointer
from app.domain.models import Chunk, Hit
from app.graph.build import build_graph
from app.graph.runner import run_chat
from app.retrieval.rerank_gate import RerankGate
from app.scope import COVERED_LAWS
from tests.conftest import ADMIN_HASH, TEST_HASH, FakeLlm, deps_with, settings_factory


def container_settings(tmp_path: Path, **overrides):
    """SQLite 파일 두 개를 tmp_path 안에 두는 설정(실제 data/ 폴더를 건드리지 않는다)."""
    return settings_factory(
        sqlite_path=str(tmp_path / "app.db"), checkpoint_path=str(tmp_path / "checkpoints.db"), **overrides
    )


@pytest.fixture
def no_network(monkeypatch):
    """소켓 연결을 시도하는 순간 테스트를 실패시킨다."""

    def refuse(*args, **kwargs):
        raise AssertionError("조립 코드가 네트워크를 썼습니다")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


# 설정의 값이 진짜 어댑터에 빠짐없이 전달되는지 확인한다. 특히 검색 깊이(search_depth)는 포트에 자리가 없어서
# 조립 코드가 OpenSearchStore 생성자로 넘기기로 했던 값이다(원장 Pre-flight 판정).
def test_build_deps_wires_settings_into_the_real_adapters(tmp_path):
    settings = container_settings(
        tmp_path,
        search_depth=123,
        rerank_monthly_cap=77,
        llm_model="llm-x",
        embedding_model="embed-x",
        embedding_dim=256,
        index_name="index-x",
    )
    deps = build_deps(settings, AppStore(settings.sqlite_path))
    assert isinstance(deps.llm, GeminiLlm) and deps.llm.model == "llm-x"
    assert isinstance(deps.embedder, GeminiEmbedder) and (deps.embedder.model, deps.embedder.dim) == ("embed-x", 256)
    assert isinstance(deps.search, OpenSearchStore) and (deps.search._depth, deps.search._index) == (123, "index-x")
    assert isinstance(deps.rerank, RerankGate) and deps.rerank._cap == 77
    assert isinstance(deps.rerank._reranker, CohereReranker)
    assert deps.settings is settings and deps.covered_laws == list(COVERED_LAWS)


# 리랭크 월 호출 수는 앱과 같은 SQLite(AppStore)에 센다. 따로 하나를 더 만들지 않고 받은 것을 그대로 쓴다.
def test_rerank_gate_counts_in_the_given_store(tmp_path):
    store = AppStore(str(tmp_path / "app.db"))
    deps = build_deps(container_settings(tmp_path), store)
    assert deps.rerank._store is store


# 진짜 부품으로 그래프를 만들어도 소켓 연결을 한 번도 하지 않는다.
def test_factory_builds_a_graph_without_touching_the_network(tmp_path, no_network):
    graph = build_graph_factory(container_settings(tmp_path))()
    assert hasattr(graph, "stream")


# 체크포인트는 앱 DB와 다른 파일에 저장한다(prune_checkpoints.py가 그 파일만 정리한다).
def test_checkpoints_live_in_their_own_file(tmp_path, no_network):
    build_graph_factory(container_settings(tmp_path))()
    assert (tmp_path / "checkpoints.db").exists()


# 체크포인터가 우리 직렬화기(허용 목록)를 쓰는지 확인하는 시험. 실측(2026-11-18, langgraph 1.2.22): 직렬화기를 지정하지 않아도
# 지금은 Hit/Chunk가 제대로 복원되지만, "등록되지 않은 타입이며 앞으로 차단될 예정"이라는 경고를 낸다. 차단되는 버전으로
# 올라가면 저장된 객체가 dict로 복원되어 같은 대화의 다음 턴이 `hit.chunk.text`에서 죽는다. 그래서 경고가 없어야 한다.
# 새 인스턴스로 다시 열어도 이어지는지(서버 재시작 뒤에도 대화가 남는지)까지 본다.
def test_checkpointer_restores_domain_objects_quietly_and_survives_restart(tmp_path, caplog):
    path = str(tmp_path / "checkpoints.db")
    thread = str(uuid.uuid4())
    graph = build_graph(deps_with(FakeLlm.script()), make_checkpointer(path))
    with caplog.at_level(logging.WARNING):
        list(run_chat(graph, thread, "주휴수당이 뭐야?"))
        state = graph.get_state({"configurable": {"thread_id": thread}}).values
    assert isinstance(state["reranked"][0], Hit) and isinstance(state["reranked"][0].chunk, Chunk)
    assert "unregistered type" not in caplog.text

    llm_after_restart = FakeLlm.script()
    restarted = build_graph(deps_with(llm_after_restart), make_checkpointer(path))
    list(run_chat(restarted, thread, "그럼 알바는요?"))
    analysis = [p.user for p in llm_after_restart.prompts if p.schema.__name__ == "Analysis"]
    assert "주휴수당이 뭐야?" in analysis[0]


# ---- create_app()을 인자 없이 부르기 (uvicorn --factory가 이렇게 부른다) ----

REQUIRED_ENV = {
    "LABOR_LAW_API_OC": "env-oc",
    "LABOR_GEMINI_API_KEY": "env-gemini",
    "LABOR_COHERE_API_KEY": "env-cohere",
    "LABOR_OPENSEARCH_PASSWORD": "env-os",
    "LABOR_SESSION_SECRET": "env-session-secret",
    "LABOR_LANGFUSE_PUBLIC_KEY": "env-lf-public",
    "LABOR_LANGFUSE_SECRET_KEY": "env-lf-secret",
    "LABOR_LANGFUSE_HOST": "https://langfuse.invalid",
    "LABOR_ADMIN_PASSWORD_HASH": ADMIN_HASH,
    "LABOR_TEST_PASSWORD_HASH": TEST_HASH,
}


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    """프로젝트의 실제 .env와 PC의 LABOR_ 환경 변수를 모두 가린다(작업 폴더를 빈 tmp_path로 옮기고 변수를 지운다)."""
    monkeypatch.chdir(tmp_path)
    for name in [n for n in os.environ if n.startswith("LABOR_")]:
        monkeypatch.delenv(name)
    return monkeypatch


# 인자 없는 create_app()이 환경 변수의 설정과 해시로 앱과 두 계정을 만든다.
def test_create_app_without_arguments_reads_the_environment(clean_env, no_network):
    for name, value in REQUIRED_ENV.items():
        clean_env.setenv(name, value)
    app = create_app()
    assert app.state.settings.session_secret == "env-session-secret"
    assert app.state.store.get_user("admin") is not None


# 필수 값이 하나라도 없으면 첫 요청이 아니라 시작할 때 멈춘다.
def test_create_app_fails_at_startup_when_a_required_setting_is_missing(clean_env):
    for name, value in REQUIRED_ENV.items():
        if name != "LABOR_SESSION_SECRET":
            clean_env.setenv(name, value)
    with pytest.raises(ValidationError):
        create_app()
