"""`GET /api/health`가 OpenSearch 상태와 색인 문서 수까지 알려 주는지 시험한다 (Task 18).

배포 뒤에 "서버는 떠 있는데 검색엔진이 죽었다"를 한 번의 호출로 구분하려는 것이다. 그래서 이 엔드포인트는
검색엔진이 죽어도 **절대 500을 내지 않고** `opensearch: "down"`으로 답해야 한다.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from app.adapters.opensearch_store import OpenSearchStore
from app.container import build_health_probe
from tests.api_support import make_app, make_client
from tests.conftest import settings_factory


def broken_probe():
    """OpenSearch가 죽었을 때처럼 예외를 내는 상태 확인 함수. 메시지에 비밀이 섞여 있어도 응답에 새면 안 된다."""
    raise ConnectionError("connection refused for admin:S3cret-pw@127.0.0.1:9200")


# 색인 문서 수와 검색엔진 상태를 그대로 알려 준다(로그인 없이).
async def test_health_reports_opensearch_and_doc_count(tmp_path):
    app = make_app(tmp_path, health_probe=lambda: ("green", 3))
    async with make_client(app) as client:
        response = await client.get("/api/health")
    assert (response.status_code, response.json()) == (200, {"status": "ok", "opensearch": "green", "index_docs": 3})


# 검색엔진이 죽어도 상태 확인은 죽지 않고 "down"과 0을 돌려준다. 앱 자체는 살아 있으므로 status는 ok다.
async def test_health_reports_down_without_crashing(tmp_path):
    app = make_app(tmp_path, health_probe=broken_probe)
    async with make_client(app) as client:
        response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "opensearch": "down", "index_docs": 0}


# 오류 메시지(접속 정보가 들어 있을 수 있다)는 로그인 없이 부를 수 있는 응답에 실리지 않는다.
async def test_health_error_text_never_reaches_the_response(tmp_path):
    app = make_app(tmp_path, health_probe=broken_probe)
    async with make_client(app) as client:
        response = await client.get("/api/health")
    assert "S3cret" not in response.text and "connection refused" not in response.text


# yellow(복제본 없는 단일 노드의 정상 상태)와 red는 그대로 전달하고, 약속에 없는 값은 "down"으로 접는다.
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("green", "green"), ("yellow", "yellow"), ("red", "red"), ("unknown", "down"), ("", "down")],
)
async def test_health_status_values_follow_the_contract(tmp_path, raw, expected):
    app = make_app(tmp_path, health_probe=lambda: (raw, 5))
    async with make_client(app) as client:
        assert (await client.get("/api/health")).json()["opensearch"] == expected


# 상태를 확인할 검색엔진이 없는 앱(그래프를 직접 넘긴 시험용 경로)은 "down"이라고 솔직히 답한다. "green"이라고 지어내지 않는다.
async def test_app_without_a_probe_reports_down(tmp_path):
    from app.api.main import create_app
    from tests.conftest import deps_with, FakeLlm
    from app.graph.build import build_graph
    from langgraph.checkpoint.memory import InMemorySaver

    settings = settings_factory(sqlite_path=str(tmp_path / "app.db"), checkpoint_path=str(tmp_path / "c.db"))
    app = create_app(settings, lambda: build_graph(deps_with(FakeLlm.script(), settings=settings), InMemorySaver()))
    async with make_client(app) as client:
        assert (await client.get("/api/health")).json() == {"status": "ok", "opensearch": "down", "index_docs": 0}


# ---- OpenSearchStore.health (가짜 클라이언트) ----


class HealthClient:
    """cluster.health와 count만 가진 가짜 opensearch-py 클라이언트. 받은 인자를 `calls`에 적는다."""

    def __init__(self, *, status="green", count=732, fail=False):
        self.calls: list[tuple[str, dict]] = []
        self._status, self._count, self._fail = status, count, fail
        self.cluster = SimpleNamespace(health=self._health)

    def _health(self, **kwargs):
        self.calls.append(("health", kwargs))
        if self._fail:
            raise ConnectionError("down")
        return {"status": self._status}

    def count(self, **kwargs):
        self.calls.append(("count", kwargs))
        return {"count": self._count}


# 색인 이름으로 좁혀서 묻고(다른 색인의 상태에 휘둘리지 않게), 응답이 오래 걸리면 기다리지 않는다.
def test_store_health_asks_about_its_own_index_with_a_short_timeout():
    client = HealthClient(status="yellow", count=732)
    assert OpenSearchStore(client, "labor-rag-v1").health() == ("yellow", 732)
    health_kwargs, count_kwargs = client.calls[0][1], client.calls[1][1]
    assert health_kwargs["index"] == "labor-rag-v1" and count_kwargs["index"] == "labor-rag-v1"
    assert 0 < health_kwargs["request_timeout"] <= 5 and 0 < count_kwargs["request_timeout"] <= 5


# 검색엔진이 응답하지 않으면 예외가 그대로 올라온다(삼키는 일은 엔드포인트가 한다. 어댑터가 "정상"을 지어내지 않게).
def test_store_health_raises_when_the_client_fails():
    with pytest.raises(ConnectionError):
        OpenSearchStore(HealthClient(fail=True), "idx").health()


# ---- 운영 조립 ----


# 운영용 상태 확인 함수는 설정의 색인 이름을 쓰고, 만들 때 네트워크를 쓰지 않는다(서버가 켜질 때 OpenSearch가 아직 없어도 된다).
def test_build_health_probe_uses_the_configured_index_without_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("조립 코드가 네트워크를 썼습니다")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    probe = build_health_probe(settings_factory(index_name="index-x"))
    assert isinstance(probe.__self__, OpenSearchStore) and probe.__self__._index == "index-x"
