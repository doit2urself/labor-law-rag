"""외부 서비스(Gemini·Cohere) 호출에 시간 제한이 실제로 걸리는지 시험한다 (최종 검토 지적 I-1, M-2).

왜 필요한가: 시간 제한을 주지 않으면 Gemini가 응답을 멈췄을 때 웹 요청 하나가 스레드를 계속 붙잡고, 사용자에게는 안내도 환불도 나가지
않는다(설치된 라이브러리 기본값 확인: LangChain `timeout=None`, Cohere는 300초에 라이브러리 자체 재시도 2번). 또 무료 한도가 소진된 상황에서
임베딩 단계가 최대 약 2분을 기다린 뒤에야 안내를 보여 줄 수 있었다(LLM은 20초로 줄였는데 임베더만 색인용 값 그대로였다).

여기서는 (1) 설정 기본값, (2) 어댑터가 진짜 라이브러리 클래스에 제한 시간을 넘기는지(가짜로 가로채서 받은 인자를 확인),
(3) 진짜 라이브러리가 우리가 넘기는 인자를 받아들이는지(네트워크 없이), (4) 서비스 조립이 설정값을 어댑터에 전달하는지를 확인한다.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from app.adapters import gemini as gemini_module
from app.adapters.cohere_rerank import CohereReranker
from app.adapters.gemini import GeminiEmbedder, GeminiLlm
from app.adapters.sqlite_store import AppStore
from app.container import build_deps
from tests.conftest import sample_hits, settings_factory


class Spy:
    """생성자 호출을 가로채 받은 인자를 `kwargs`에 적어 두는 가짜 클래스(진짜 클라이언트를 만들지 않는다)."""

    kwargs: dict = {}

    def __init__(self, *args, **kwargs):
        type(self).kwargs = kwargs


def new_spy() -> type[Spy]:
    return type("SpyClient", (Spy,), {"kwargs": {}})


# 설정 기본값: 값은 "시작값"이다. 근거: 서버 평가(docs/results.md)에서 질문 하나 전체가 5.9초 이하(첫 질문 72.66초 한 건은 원인 미상)였고
# 한 질문은 LLM 호출 약 3회 + 임베딩 1회 + 리랭크 1회다. 호출 하나의 값은 따로 재지 않았다.
def test_settings_have_explicit_timeouts():
    s = settings_factory()
    assert (s.llm_timeout_s, s.embedding_timeout_s, s.rerank_timeout_s) == (30.0, 15.0, 15.0)


# LLM 클라이언트: LangChain은 초 단위를 받는다(내부에서 밀리초로 바꾼다). SDK 자체 재시도 없음(max_retries=1)은 그대로다.
def test_llm_client_is_built_with_the_timeout(monkeypatch):
    spy = new_spy()
    monkeypatch.setattr(gemini_module, "ChatGoogleGenerativeAI", spy)
    GeminiLlm("k", "m", timeout_s=7.5)._get_client()
    assert spy.kwargs["timeout"] == 7.5 and spy.kwargs["max_retries"] == 1


# 임베딩 클라이언트: google-genai의 HttpOptions.timeout은 **밀리초**다(설치된 SDK의 필드 설명 확인).
def test_embedder_client_gets_the_timeout_in_milliseconds(monkeypatch):
    import google.genai

    spy = new_spy()
    monkeypatch.setattr(google.genai, "Client", spy)
    GeminiEmbedder("k", timeout_s=7.5)._get_client()
    assert spy.kwargs["http_options"].timeout == 7500


# 색인용 임베더(시간 제한을 안 준 경우)는 예전과 똑같이 동작한다: 큰 묶음을 오래 기다려야 할 수 있어서 제한을 걸지 않는다.
def test_embedder_without_a_timeout_keeps_the_old_behaviour(monkeypatch):
    import google.genai

    spy = new_spy()
    monkeypatch.setattr(google.genai, "Client", spy)
    GeminiEmbedder("k")._get_client()
    assert "http_options" not in spy.kwargs


# 진짜 라이브러리가 우리가 넘기는 인자를 받아들이는지(가짜로만 시험하면 인자 이름이 틀려도 통과한다). 네트워크는 쓰지 않는다.
def test_real_sdk_classes_accept_the_timeout_arguments(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("클라이언트를 만들 때 네트워크를 썼습니다")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)

    chat = GeminiLlm("k", "gemini-x", timeout_s=7.5)._get_client()
    assert chat.timeout == 7.5
    embed_client = GeminiEmbedder("k", timeout_s=7.5)._get_client()
    assert embed_client is not None


# Cohere: 클라이언트에 제한 시간을 주고, 요청마다 라이브러리 자체 재시도를 끈다(429·5xx를 몰래 두 번 더 보내면 월 호출 수가 어긋나고
# 최악의 경우 폴백이 몇 분 뒤에야 시작된다. 재시도는 한도 게이트 한 곳에서만 판단한다).
def test_cohere_client_gets_the_timeout_and_requests_have_no_sdk_retries(monkeypatch):
    import cohere

    spy = new_spy()
    monkeypatch.setattr(cohere, "ClientV2", spy)
    CohereReranker("k", timeout_s=7.5)
    assert spy.kwargs["timeout"] == 7.5

    seen: dict = {}

    class FakeClient:
        def rerank(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(results=[SimpleNamespace(index=0, relevance_score=0.9)])

    CohereReranker("k", client=FakeClient()).rerank("q", sample_hits(3), 1)
    assert seen["request_options"] == {"max_retries": 0}


# 서비스 조립: 설정의 제한 시간을 세 어댑터에 전달하고, 웹 요청용 임베더의 재시도는 LLM과 같게(2회·최대 20초) 줄인다.
def test_container_wires_timeouts_and_the_short_web_retry(tmp_path):
    settings = settings_factory(
        sqlite_path=str(tmp_path / "app.db"), llm_timeout_s=11.0, embedding_timeout_s=12.0, rerank_timeout_s=13.0
    )
    deps = build_deps(settings, AppStore(settings.sqlite_path))
    assert deps.llm._timeout_s == 11.0
    assert deps.embedder._timeout_s == 12.0
    assert deps.rerank._reranker._timeout_s == 13.0
    assert (deps.embedder._max_retries, deps.embedder._max_wait_s) == (2, 20.0)
