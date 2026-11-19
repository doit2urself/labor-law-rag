"""API 테스트 공용 도구: 가짜 부품으로 만든 앱, 접속 주소를 정해서 요청하는 클라이언트, SSE 해석기.

왜 필요한가: API 테스트(test_api_*.py)는 모두 "앱을 만들고, 로그인하고, 특정 IP에서 질문을 보내고, 돌아온 이벤트를
읽는" 같은 동작을 한다. 이 파일이 그 반복을 맡아 각 테스트가 시험할 내용만 남게 한다.

그래프는 가짜가 아니라 **진짜 LangGraph**이고, 그 안의 부품(LLM·검색·리랭커)과 체크포인트만 가짜다. 그래서 API 테스트가
"요청 → 그래프 → SSE 이벤트"의 이어짐까지 함께 시험한다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from langgraph.checkpoint.memory import InMemorySaver

from app.api.auth import SESSION_COOKIE
from app.api.main import create_app
from app.graph.build import build_graph
from app.graph.state import checkpoint_serde
from tests.conftest import FakeLlm, deps_with, settings_factory

# 테스트의 기본 접속 주소. 203.0.113.0/24는 문서·예제용으로 예약된 대역(TEST-NET-3)이라 실제 서버와 겹치지 않는다.
PEER_IP = "203.0.113.5"

# 세션 쿠키에 Secure 속성이 붙으므로 주소가 https여야 쿠키 저장소가 쿠키를 다시 보내 준다.
BASE_URL = "https://test"


def make_app(
    tmp_path: Path,
    *,
    llm: Any = None,
    graph_factory: Callable[[], Any] | None = None,
    tracing: Any = None,
    health_probe: Callable[[], tuple[str, int]] | None = None,
    **settings_overrides: Any,
) -> FastAPI:
    """가짜 부품 그래프로 앱을 만든다. SQLite 파일은 모두 tmp_path 안(실제 data/ 폴더를 건드리지 않는다).

    llm: 그래프가 쓸 가짜 LLM(없으면 평범한 상담 시나리오). 호출 수를 세려면 호출한 쪽이 직접 만들어 넘기고 들고 있는다.
    graph_factory: 그래프 조립을 통째로 바꿀 때만 쓴다(없으면 위의 llm으로 진짜 그래프를 만든다).
    tracing: 관측(Langfuse) 연결. 없으면 create_app이 꺼진 연결(NullTracing)을 끼운다.
    health_probe: `/api/health`가 부르는 검색엔진 상태 확인 함수 `() -> (상태, 문서 수)`. 없으면 초록 불에 문서 3개인 가짜를 끼운다.
    settings_overrides: Settings 값을 바꾼다. 예: `make_app(tmp_path, test_ip_limit=3)`.
    """
    settings = settings_factory(
        sqlite_path=str(tmp_path / "app.db"),
        checkpoint_path=str(tmp_path / "checkpoints.db"),
        **settings_overrides,
    )
    if graph_factory is None:
        the_llm = llm if llm is not None else FakeLlm.script()

        def graph_factory() -> Any:
            return build_graph(deps_with(the_llm, settings=settings), InMemorySaver(serde=checkpoint_serde()))

    return create_app(settings, graph_factory, tracing, health_probe or (lambda: ("green", 3)))


def make_client(app: FastAPI, cookie: str | None = None, peer: tuple[str, int] = (PEER_IP, 5000)) -> httpx.AsyncClient:
    """앱을 메모리 안에서 부르는 클라이언트. `peer`는 서버가 보게 될 접속 주소(소켓 주소)다.

    `async with make_client(...) as c:` 로 쓴다. cookie를 주면 그 세션으로 로그인한 상태가 된다.
    """
    transport = httpx.ASGITransport(app=app, client=peer)
    client = httpx.AsyncClient(transport=transport, base_url=BASE_URL)
    if cookie is not None:
        client.cookies.set(SESSION_COOKIE, cookie)
    return client


async def login_cookie(app: FastAPI, username: str, password: str) -> str:
    """로그인 API로 로그인해 세션 쿠키 값을 돌려준다(로그인이 실패하면 테스트를 멈춘다)."""
    async with make_client(app) as client:
        response = await client.post("/api/login", json={"username": username, "password": password})
        assert response.status_code == 200, response.text
        return client.cookies[SESSION_COOKIE]


async def post_chat(
    app: FastAPI,
    cookie: str | None,
    question: str = "q",
    *,
    peer: tuple[str, int] = (PEER_IP, 5000),
    thread_id: str | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """질문 한 건을 보내고 응답 전체를 돌려준다(요청마다 새 클라이언트라 동시 요청 시험에도 쓴다)."""
    body: dict[str, Any] = {"question": question}
    if thread_id is not None:
        body["thread_id"] = thread_id
    async with make_client(app, cookie, peer) as client:
        return await client.post("/api/chat", json=body, headers=headers)


def parse_sse(text: str) -> list[tuple[str, dict]]:
    """SSE 본문을 `(이벤트 이름, 데이터)` 목록으로 풀어 준다. 이벤트는 빈 줄로 나뉘고 `event:`·`data:` 줄을 가진다."""
    events: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        name = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        if name is not None:
            events.append((name, data))
    return events
