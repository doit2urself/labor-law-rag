"""리랭커 어댑터(CohereReranker)와 월 호출 한도 게이트(RerankGate)를 검증한다.

지키려는 것: **월 청구액 0원**. Cohere 시험 키는 월 1,000회까지 무료이므로, 서버가 호출 수를 직접 세다가
950회에 이르면 리랭크를 끄고 RRF 상위 5개로 대신한다(스펙 6.3). 리랭크가 실패해도 답변은 계속 나가야 한다.
네트워크는 쓰지 않는다. Cohere 클라이언트 자리에는 같은 메서드 이름의 가짜 객체를 넣는다.
"""

import sqlite3
from contextlib import closing
from datetime import date
from types import SimpleNamespace

import cohere
import pytest

from app.adapters.cohere_rerank import CohereReranker
from app.adapters.sqlite_store import AppStore
from app.domain.errors import ExternalServiceError, QuotaExceeded
from app.domain.models import Chunk, Hit
from app.ports import RerankerPort, RerankStage
from app.retrieval.rerank_gate import RerankGate
from tests.conftest import FakeReranker

TODAY = lambda: date(2026, 11, 18)  # noqa: E731  (게이트의 "오늘"을 고정해 달이 바뀌는 상황을 시험한다)


def make_hit(n: int) -> Hit:
    """RRF가 준 순서로 n번째(1부터) 후보. 본문에 번호를 넣어 어느 후보인지 알아볼 수 있게 한다."""
    chunk = Chunk(
        chunk_id=f"law:1:{n}:0:20250101", doc_type="law", title=f"근로기준법 제{n}조", text=f"본문 {n}",
        law_name="근로기준법", article_no=str(n), paragraph=None, effective_date="2025-01-01",
        promulgation_date="2024-12-01", source_id="law:1", source_url=None, extra={},
    )
    return Hit(chunk=chunk, score=1.0 / n, rank=n)


def hits20() -> list[Hit]:
    return [make_hit(n) for n in range(1, 21)]


# ---- CohereReranker ----


class FakeCohereClient:
    """cohere.ClientV2.rerank와 같은 이름·인자를 가진 가짜. 정해 둔 (index, 점수) 목록을 응답처럼 돌려준다."""

    def __init__(self, results=None, error: Exception | None = None):
        self._results = results or []
        self._error = error
        self.last_kwargs: dict = {}

    def rerank(self, **kwargs):
        self.last_kwargs = kwargs
        if self._error:
            raise self._error
        return SimpleNamespace(results=[SimpleNamespace(index=i, relevance_score=s) for i, s in self._results])


# 응답의 index로 원래 후보를 찾아 점수를 넣고, 점수 높은 순으로 순위를 1부터 다시 매긴다.
def test_reranker_maps_index_back_to_hits_and_sets_score_and_rank():
    client = FakeCohereClient(results=[(2, 0.9), (0, 0.4)])
    out = CohereReranker("k", client=client).rerank("주휴수당", hits20()[:3], top_n=2)
    assert [h.chunk.article_no for h in out] == ["3", "1"]
    assert [h.score for h in out] == [0.9, 0.4] and [h.rank for h in out] == [1, 2]


# 응답 순서를 믿지 않고 점수로 직접 정렬한다(응답이 정렬돼 있다는 보장은 문서로 확인하지 못했다).
def test_reranker_sorts_by_score_even_if_response_is_unsorted():
    client = FakeCohereClient(results=[(0, 0.1), (1, 0.8), (2, 0.5)])
    out = CohereReranker("k", client=client).rerank("q", hits20()[:3], top_n=3)
    assert [h.chunk.article_no for h in out] == ["2", "3", "1"]


# 요청에는 모델 이름, 질문, 후보 본문(청크 text), top_n이 그대로 실린다.
def test_reranker_sends_model_query_documents_and_top_n():
    client = FakeCohereClient(results=[(0, 0.5)])
    CohereReranker("k", model="rerank-x", client=client).rerank("질문", hits20()[:2], top_n=1)
    assert client.last_kwargs == {"model": "rerank-x", "query": "질문", "documents": ["본문 1", "본문 2"], "top_n": 1}


# 기본 모델 이름은 Cohere 공식 문서의 Rerank 5 목록에 있는 `rerank-v5.0-pro`다.
def test_reranker_default_model_is_rerank_v5_pro():
    client = FakeCohereClient(results=[(0, 0.5)])
    CohereReranker("k", client=client).rerank("q", hits20()[:1], top_n=1)
    assert client.last_kwargs["model"] == "rerank-v5.0-pro"


# 후보가 없으면 호출하지 않는다(호출 한도를 아낀다).
def test_reranker_does_not_call_api_for_empty_candidates():
    client = FakeCohereClient()
    assert CohereReranker("k", client=client).rerank("q", [], top_n=5) == []
    assert client.last_kwargs == {}


# top_n이 후보 수보다 크면 후보 수로 줄여서 보낸다.
def test_reranker_caps_top_n_at_candidate_count():
    client = FakeCohereClient(results=[(0, 0.5)])
    CohereReranker("k", client=client).rerank("q", hits20()[:2], top_n=5)
    assert client.last_kwargs["top_n"] == 2


# 429는 QuotaExceeded, 그 밖의 Cohere 오류는 ExternalServiceError로 바뀐다(게이트는 이 둘만 알면 된다).
def test_reranker_translates_cohere_errors():
    with pytest.raises(QuotaExceeded):
        CohereReranker("k", client=FakeCohereClient(error=cohere.TooManyRequestsError(body={"message": "x"}))).rerank(
            "q", hits20()[:2], 1
        )
    with pytest.raises(ExternalServiceError):
        CohereReranker("k", client=FakeCohereClient(error=cohere.InternalServerError(body={"message": "x"}))).rerank(
            "q", hits20()[:2], 1
        )


# 응답이 후보 범위 밖의 index를 주면 조용히 넘기지 않고 오류로 알린다.
def test_reranker_rejects_out_of_range_index():
    client = FakeCohereClient(results=[(7, 0.9)])
    with pytest.raises(ExternalServiceError):
        CohereReranker("k", client=client).rerank("q", hits20()[:2], 1)


# 포트 규약을 지킨다.
def test_adapter_and_gate_satisfy_ports(tmp_path):
    assert isinstance(CohereReranker("k", client=FakeCohereClient()), RerankerPort)
    assert isinstance(RerankGate(FakeReranker(), AppStore(str(tmp_path / "a.db"))), RerankStage)


# ---- RerankGate ----


# 호출 수가 한도(cap)에 이르면 리랭커를 부르지 않고 RRF 상위 5개로 대신한다.
def test_gate_blocks_at_cap(tmp_path):
    store = AppStore(str(tmp_path / "a.db"))
    gate = RerankGate(FakeReranker(), store, cap=3, today=lambda: date(2026, 11, 18))
    reasons = [gate.rerank("q", hits20(), 5).reason for _ in range(4)]
    assert reasons == ["ok", "ok", "ok", "cap"]


# 한도에 막혔을 때는 리랭커를 실제로 부르지 않는다(비용이 드는 호출을 막는 것이 목적).
def test_gate_does_not_call_reranker_after_cap(tmp_path):
    reranker = FakeReranker()
    gate = RerankGate(reranker, AppStore(str(tmp_path / "a.db")), cap=2, today=TODAY)
    for _ in range(5):
        gate.rerank("q", hits20(), 5)
    assert reranker.calls == 2


# 리랭커가 실패하면 기존(RRF) 순서의 상위 5개를 돌려준다. 답변은 계속 나가야 한다.
def test_gate_falls_back_to_rrf_order_on_error(tmp_path):
    r = RerankGate(FakeReranker(raises=QuotaExceeded()), AppStore(str(tmp_path / "a.db"))).rerank("q", hits20(), 5)
    assert (r.used_reranker, r.reason) == (False, "error") and [h.rank for h in r.hits] == [1, 2, 3, 4, 5]


# 외부 서비스 오류(시간 초과 등)도 같은 방식으로 폴백한다.
def test_gate_falls_back_on_external_service_error(tmp_path):
    r = RerankGate(FakeReranker(raises=ExternalServiceError("timeout")), AppStore(str(tmp_path / "a.db"))).rerank(
        "q", hits20(), 5
    )
    assert (r.used_reranker, r.reason) == (False, "error") and len(r.hits) == 5


# 실패한 시도도 호출 수에 센다(서비스 쪽에서는 이미 요청이 나간 것이므로 한도를 갉아먹는다).
def test_failed_attempts_count_toward_cap(tmp_path):
    store = AppStore(str(tmp_path / "a.db"))
    gate = RerankGate(FakeReranker(raises=QuotaExceeded()), store, today=lambda: date(2026, 11, 18))
    gate.rerank("q", hits20(), 5)
    assert store.get_rerank_calls("2026-11") == 1


# 한도에 막힌 호출은 세지 않는다(실제로 나간 요청만 센다).
def test_blocked_calls_are_not_counted(tmp_path):
    store = AppStore(str(tmp_path / "a.db"))
    gate = RerankGate(FakeReranker(), store, cap=1, today=TODAY)
    gate.rerank("q", hits20(), 5)
    gate.rerank("q", hits20(), 5)
    assert store.get_rerank_calls("2026-11") == 1


# 달이 바뀌면 호출 수가 새로 시작한다.
def test_counter_resets_next_month(tmp_path):
    store = AppStore(str(tmp_path / "a.db"))
    d = {"v": date(2026, 11, 30)}
    gate = RerankGate(FakeReranker(), store, cap=1, today=lambda: d["v"])
    gate.rerank("q", hits20(), 5)
    d["v"] = date(2026, 12, 1)
    assert gate.rerank("q", hits20(), 5).reason == "ok"


# 정상일 때는 리랭커가 고른 결과와 used_reranker=True를 돌려준다.
def test_gate_ok_returns_reranked_hits(tmp_path):
    r = RerankGate(FakeReranker(), AppStore(str(tmp_path / "a.db")), today=TODAY).rerank("q", hits20(), 5)
    assert (r.used_reranker, r.reason, len(r.hits)) == (True, "ok", 5)


# 한도 기본값은 스펙의 950회다(Cohere 월 1,000회에서 50회를 남긴다).
def test_gate_default_cap_is_950(tmp_path):
    db = str(tmp_path / "a.db")
    store = AppStore(db)
    # 949번을 일일이 올리면 커밋 949번이라 느리다. 이미 949번 쓴 상태를 파일에 바로 만든다.
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("INSERT INTO rerank_calls(month, calls) VALUES ('2026-11', 949)")
    gate = RerankGate(FakeReranker(), store, today=TODAY)
    assert gate.rerank("q", hits20(), 5).reason == "ok"  # 950번째 시도는 허용
    assert gate.rerank("q", hits20(), 5).reason == "cap"  # 951번째부터 막힘
