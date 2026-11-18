"""OpenSearch 통합 테스트: 실제 서버의 OpenSearch가 있어야 돈다(`pytest -m integration`).

기본 실행(`pytest`)에서는 pytest.ini의 `-m "not integration"` 때문에 빠진다. 서버에 직접 접속하거나,
이 PC에서 SSH 터널(`ssh -N -L 19200:127.0.0.1:9200 contabo`)을 열고 `LABOR_OPENSEARCH_URL`을 터널 주소로
지정해 돌린다. 접속 정보는 `.env`/환경 변수의 LABOR_OPENSEARCH_URL(기본 https://127.0.0.1:9200),
LABOR_OPENSEARCH_USER(기본 admin), LABOR_OPENSEARCH_PASSWORD(필수, 없으면 건너뜀)에서 읽는다.

운영 색인(labor-rag-v1)을 건드리지 않도록 테스트마다 임시 색인·임시 파이프라인을 만들고 끝나면 지운다.
이 파일이 풀어 주는 "문서로 확인하지 못한 것": faiss HNSW + cosinesimil 색인이 만들어지는지(ensure_index),
RRF 파이프라인이 실제로 순위를 합치는지, pagination_depth가 RRF 아래에서 동작하는지.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest

from app.adapters.opensearch_store import OpenSearchStore, make_client
from app.domain.models import Chunk
from scripts.envfile import read_env
from tests.conftest import FakeEmbedder

pytestmark = pytest.mark.integration

DIM = 768
_SOURCE = "law:근로기준법:436:20250223"


def make_chunk(article_no: str, text: str, *, effective_date: str = "2025-02-23",
               source_id: str = _SOURCE) -> Chunk:
    """테스트용 청크. 필드 모양은 실제 파서가 만드는 값과 같다."""
    return Chunk(
        chunk_id=f"{source_id}:{article_no}:0",
        doc_type="law",
        title=f"근로기준법 제{article_no}조",
        text=text,
        law_name="근로기준법",
        article_no=article_no,
        paragraph=None,
        effective_date=effective_date,
        promulgation_date="2025-02-21",
        source_id=source_id,
        source_url=None,
        extra={"embedding_model": "fake", "embedding_dim": DIM},
    )


CH55 = make_chunk("55", "제55조(휴일) 사용자는 근로자에게 1주일에 평균 1회 이상의 유급휴일을 보장하여야 한다.")
CH56 = make_chunk("56", "제56조(연장·야간 및 휴일 근로) 연장근로에 대하여는 통상임금의 100분의 50 이상을 가산하여 지급하여야 한다.")
CH17 = make_chunk("17", "제17조(근로조건의 명시) 사용자는 근로계약을 체결할 때에 근로자에게 임금을 명시하여야 한다.")
THREE = [CH55, CH56, CH17]


def vec(text: str) -> list[float]:
    """같은 글이면 같은 가짜 벡터. 의미 검색의 정확도가 아니라 '배선'을 시험하려는 용도다."""
    return FakeEmbedder(DIM).embed_query(text)


@pytest.fixture(scope="module")
def client():
    env = read_env()
    password = env.get("LABOR_OPENSEARCH_PASSWORD")
    if not password:
        pytest.skip("LABOR_OPENSEARCH_PASSWORD가 없어 통합 테스트를 건너뜁니다")
    return make_client(
        env.get("LABOR_OPENSEARCH_URL", "https://127.0.0.1:9200"),
        env.get("LABOR_OPENSEARCH_USER", "admin"),
        password,
    )


@pytest.fixture
def store(client):
    """임시 색인·파이프라인이 붙은 OpenSearchStore. 테스트가 끝나면 둘 다 지운다."""
    tag = uuid.uuid4().hex[:8]
    index, pipeline = f"labor-rag-itest-{tag}", f"rrf-itest-{tag}"
    s = OpenSearchStore(client, index, pipeline)
    s.ensure_index(DIM)
    yield s
    with contextlib.suppress(Exception):
        client.indices.delete(index=index)
    with contextlib.suppress(Exception):
        client.search_pipeline.delete(id=pipeline)


@pytest.fixture
def loaded(store):
    """세 조문이 색인된 store. 같은 청크를 두 번 넣어도 문서 수가 늘지 않는지도 여기서 보장한다."""
    store.bulk_upsert(THREE, [vec(c.text) for c in THREE])
    return store


# Nori가 조사를 떼는지: "뿌리가"에서 "뿌리"가 나와야 한다(스펙 6.3의 Nori 선택 이유).
def test_nori_splits_josa(client):
    tokens = [t["token"] for t in client.indices.analyze(body={"analyzer": "nori", "text": "뿌리가"})["tokens"]]
    assert "뿌리" in tokens


# 색인의 text 필드가 실제로 Nori로 분석되는지(매핑이 적용됐는지): 조사 "을"은 토큰으로 남지 않는다.
def test_index_text_field_uses_nori(store, client):
    body = {"field": "text", "text": "주휴수당을"}
    tokens = [t["token"] for t in client.indices.analyze(index=store._index, body=body)["tokens"]]
    assert "수당" in tokens and "을" not in tokens


# 조문번호가 정확히 일치하는 청크가 1등이다(계획서 Step 6의 시험).
def test_exact_article_match_ranks_first(loaded):
    hits = loaded.search("제55조", vec(CH55.text))
    assert hits[0].chunk.article_no == "55"


# 벡터가 다른 조문(제17조)을 가리켜도, 질문에 "제55조"가 있으면 55조가 1등이다(정확 일치 하위 쿼리의 역할).
def test_exact_match_beats_a_vector_that_points_elsewhere(loaded):
    hits = loaded.search("제55조", vec(CH17.text))
    assert hits[0].chunk.article_no == "55"


# RRF가 실제로 적용됐는지: 점수는 하위 쿼리별 1/(60+순위)의 합이라 아주 작다(최대 하위 쿼리 3개 × 1/61 ≈ 0.049).
def test_scores_are_rrf_sums(loaded):
    hits = loaded.search("제55조", vec(CH55.text))
    assert hits and all(0 < h.score < 0.06 for h in hits)
    assert [h.rank for h in hits] == list(range(1, len(hits) + 1))


# 시행일이 미래인 청크는 현행 검색에서 빠지고, only_current=False면 나온다.
def test_current_filter_hides_future_versions(store):
    future = make_chunk("99", "제99조(미래) 미래에 시행될 조문이다.", effective_date="2999-01-01")
    store.bulk_upsert([CH55, future], [vec(CH55.text), vec(future.text)])
    current = [h.chunk.article_no for h in store.search("제99조", vec(future.text), only_current=True)]
    everything = [h.chunk.article_no for h in store.search("제99조", vec(future.text), only_current=False)]
    assert "99" not in current and "99" in everything


# 같은 청크를 두 번 넣어도 문서가 늘지 않는다(chunk_id로 덮어쓰기).
def test_bulk_upsert_is_idempotent(loaded, client):
    loaded.bulk_upsert(THREE, [vec(c.text) for c in THREE])
    assert client.count(index=loaded._index)["count"] == 3


# 원문 단위 삭제와 목록 조회: 지운 개수를 돌려주고, 목록에서 사라진다.
def test_delete_by_source_and_list_source_ids(loaded):
    other = make_chunk("7", "제7조(강제 근로의 금지) 강제로 근로하게 하지 못한다.", source_id="law:다른법:1:20250101")
    loaded.bulk_upsert([other], [vec(other.text)])
    assert loaded.list_source_ids() == {_SOURCE, "law:다른법:1:20250101"}
    assert loaded.delete_by_source({"law:다른법:1:20250101"}) == 1
    assert loaded.list_source_ids() == {_SOURCE}
