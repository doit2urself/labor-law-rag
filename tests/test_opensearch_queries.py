"""OpenSearch 어댑터의 단위 테스트(서버 없이 이 PC에서 돈다).

검증 대상: 색인 매핑·RRF 파이프라인·하이브리드 쿼리를 만드는 순수 함수, 청크↔문서 변환,
그리고 가짜 클라이언트로 본 `OpenSearchStore`의 요청 조립. 실제 OpenSearch가 필요한 시험
(Nori 분석, 조문번호 정확 일치 순위)은 tests/test_opensearch_integration.py에서 서버에서만 한다.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from opensearchpy import OpenSearch

from app.adapters.opensearch_store import (
    OpenSearchStore,
    build_client_kwargs,
    build_hybrid_query,
    build_index_body,
    build_rrf_pipeline_body,
    chunk_to_doc,
    doc_to_chunk,
    extract_exact_terms,
)
from app.domain.errors import ExternalServiceError
from app.domain.models import Chunk
from app.ports import IndexPort, SearchPort

V = [0.1] * 768


def sample_chunk(**overrides) -> Chunk:
    """테스트용 청크 하나. 실제 파서가 만드는 값의 모양(ISO 날짜, 조문번호 문자열)을 따른다."""
    values = dict(
        chunk_id="law:근로기준법:436:20250223:55:0",
        doc_type="law",
        title="근로기준법 제55조(휴일)",
        text="제55조(휴일) ① 사용자는 근로자에게 일주일에 평균 1회 이상의 유급휴일을 보장하여야 한다.",
        law_name="근로기준법",
        article_no="55",
        paragraph=None,
        effective_date="2025-02-23",
        promulgation_date="2025-02-21",
        source_id="law:근로기준법:436:20250223",
        source_url="https://www.law.go.kr/법령/근로기준법/제55조",
        extra={"embedding_model": "gemini-embedding-2", "embedding_dim": 768},
    )
    values.update(overrides)
    return Chunk(**values)


# ---------------------------------------------------------------- 순수 함수


# 매핑: 본문은 색인과 검색에 같은 Nori 분석기를 쓰고, 벡터는 knn_vector(HNSW, m=16)이며 색인에서 knn이 켜져 있다.
def test_mapping_uses_nori_for_both_index_and_search():
    body = build_index_body(768)
    p = body["mappings"]["properties"]
    assert p["text"]["analyzer"] == "nori" and p["text"].get("search_analyzer") in (None, "nori")
    assert p["embedding"]["type"] == "knn_vector" and p["embedding"]["dimension"] == 768
    assert p["embedding"]["method"]["name"] == "hnsw"
    assert p["embedding"]["method"]["parameters"]["m"] == 16
    assert body["settings"]["index"]["knn"] is True


# 정확 일치용 필드는 분석(쪼개기)하지 않는 keyword여야 "43"과 "43의2"가 섞이지 않는다.
def test_exact_match_fields_are_keywords_and_dates_are_dates():
    p = build_index_body(768)["mappings"]["properties"]
    for field in ("law_name", "article_no", "source_id", "doc_type"):
        assert p[field]["type"] == "keyword", field
    assert p["effective_date"]["type"] == "date"
    assert "embedding_model" in p and "embedding_dim" in p


# 차원 수는 인자로 받은 값이 그대로 매핑에 들어간다(모델을 바꾸면 차원도 바뀌므로).
def test_mapping_dimension_follows_argument():
    assert build_index_body(3072)["mappings"]["properties"]["embedding"]["dimension"] == 3072


# RRF 처리기: rank_constant 60이 명시되어 있다.
def test_rrf_pipeline_has_rank_constant_60():
    s = json.dumps(build_rrf_pipeline_body())
    assert '"rrf"' in s and '"rank_constant": 60' in s


# 파이프라인은 phase_results_processors 아래 score-ranker-processor를 쓴다(공식 문서의 모양).
def test_rrf_pipeline_uses_score_ranker_processor():
    body = build_rrf_pipeline_body(rank_constant=40)
    proc = body["phase_results_processors"][0]["score-ranker-processor"]
    assert proc["combination"] == {"technique": "rrf", "rank_constant": 40}


# 하이브리드 쿼리의 모양: 하위 쿼리 1~5개, 결과 개수, query_string 미사용.
def test_hybrid_query_shape():
    q = build_hybrid_query("주휴수당", [0.1] * 768, depth=150, size=20)
    h = q["query"]["hybrid"]["queries"]
    assert 1 <= len(h) <= 5 and q["size"] == 20 and "query_string" not in json.dumps(q)


# 검색어 속 " * \ 는 쿼리 문법이 아니라 일반 문자로만 들어간다(match 쿼리의 문자열 값).
def test_special_characters_stay_plain_text():
    text = '근로기준법 제"55"조 * \\'
    dumped = json.dumps(build_hybrid_query(text, [0.0] * 768), ensure_ascii=False)
    assert json.dumps(text, ensure_ascii=False)[1:-1] in dumped


# 현행 필터: 시행일이 없는 문서는 통과시키고, 있으면 오늘 이전 시행분만 통과시킨다.
def test_current_filter_keeps_documents_without_effective_date():
    s = json.dumps(build_hybrid_query("x", [0.0] * 768, only_current=True, today="2026-11-18"))
    assert "effective_date" in s and "must_not" in s and "exists" in s


# 필터는 hybrid 바로 아래에 한 번만 둔다(공식 문서: 모든 하위 쿼리에 공통 적용). 하위 쿼리 안에는 없다.
def test_filter_is_inside_hybrid_not_inside_each_subquery():
    h = build_hybrid_query("x", V, only_current=True, today="2026-11-18")["query"]["hybrid"]
    assert "effective_date" in json.dumps(h["filter"])
    assert "effective_date" not in json.dumps(h["queries"])
    assert "2026-11-18" in json.dumps(h["filter"])


# only_current=False면 필터 키 자체가 없어야 한다(과거 버전까지 검색하는 경로).
def test_no_filter_key_when_only_current_is_false():
    assert "filter" not in build_hybrid_query("x", V, only_current=False)["query"]["hybrid"]


# 깊이: 하위 쿼리마다 depth개를 가져와야 RRF가 합칠 재료가 충분하다(knn의 k와 pagination_depth).
def test_depth_applies_to_every_subquery():
    q = build_hybrid_query("x", [0.5] * 768, depth=120, size=10)
    h = q["query"]["hybrid"]
    assert h["pagination_depth"] == 120 and q["size"] == 10
    knn = next(s for s in h["queries"] if "knn" in s)["knn"]["embedding"]
    assert knn["k"] == 120 and knn["vector"] == [0.5] * 768


# 조문번호가 없는 질문에는 정확 일치 하위 쿼리를 만들지 않는다(빈 조건이 모든 문서와 맞는 사고 방지).
def test_exact_subquery_only_when_article_number_is_present():
    plain = build_hybrid_query("주휴수당", V)["query"]["hybrid"]["queries"]
    exact = build_hybrid_query("근로기준법 제55조", V)["query"]["hybrid"]["queries"]
    assert len(exact) == len(plain) + 1
    s = json.dumps(exact, ensure_ascii=False)
    assert '"article_no"' in s and '"55"' in s and "근로기준법" in s


# 조문번호는 반드시 일치해야 하고(must), 법령명은 일치하면 점수를 더 주는 정도(should)다.
def test_exact_subquery_requires_article_and_only_boosts_law_name():
    exact = build_hybrid_query("근로기준법 제55조", V)["query"]["hybrid"]["queries"]
    bool_q = next(s for s in exact if "bool" in s)["bool"]
    assert "article_no" in json.dumps(bool_q["must"])
    assert "law_name" in json.dumps(bool_q["should"]) and "law_name" not in json.dumps(bool_q["must"])


# 질문에서 조문번호·법령명을 뽑는 규칙.
@pytest.mark.parametrize(
    "text, articles, laws",
    [
        ("근로기준법 제55조 알려줘", ["55"], ["근로기준법"]),
        ("제 55 조가 뭐야", ["55"], []),
        ("근로기준법 제43조의2", ["43의2"], ["근로기준법"]),
        ("제55조와 제56조", ["55", "56"], []),
        ("제55조 제55조", ["55"], []),
        ("주휴수당 받을 수 있어?", [], []),
        # 시행령 이름 안에 법률 이름이 들어 있어도 긴 쪽 하나만 잡는다.
        ("근로기준법 시행령 제7조", ["7"], ["근로기준법 시행령"]),
    ],
)
def test_extract_exact_terms(text, articles, laws):
    assert extract_exact_terms(text) == (articles, laws)


# 접속 인자: https 주소는 SSL을 켜고, 계정을 넘기며, 주소·포트를 나눠 담는다.
def test_client_kwargs_for_https_with_credentials():
    kw = build_client_kwargs("https://127.0.0.1:9200", "admin", "pw")
    assert kw["hosts"] == [{"host": "127.0.0.1", "port": 9200}]
    assert kw["http_auth"] == ("admin", "pw") and kw["use_ssl"] is True


# 기본값은 인증서 검증을 끈다(설치 직후의 자체 서명 인증서, 같은 서버 안 통신). 켜려면 인자로 명시한다.
def test_client_kwargs_certificate_check_is_off_by_default_and_switchable():
    assert build_client_kwargs("https://127.0.0.1:9200", "a", "b")["verify_certs"] is False
    assert build_client_kwargs("https://127.0.0.1:9200", "a", "b", verify_certs=True)["verify_certs"] is True


# http 주소면 SSL을 켜지 않는다. 포트를 생략하면 9200을 쓴다.
def test_client_kwargs_for_plain_http_defaults_port():
    kw = build_client_kwargs("http://localhost", "a", "b")
    assert kw["use_ssl"] is False and kw["hosts"] == [{"host": "localhost", "port": 9200}]


# 만든 인자는 실제 클라이언트 생성자가 받아들여야 한다(생성만 하고 접속은 하지 않는다).
def test_client_kwargs_are_accepted_by_opensearch_py():
    assert isinstance(OpenSearch(**build_client_kwargs("https://127.0.0.1:9200", "a", "b")), OpenSearch)


# ---------------------------------------------------------------- 청크 ↔ 문서 변환


# 색인 문서로 바꿨다가 되돌려도 청크의 모든 칸이 그대로여야 한다(필드 누락 방지).
def test_chunk_doc_round_trip_keeps_every_field():
    c = sample_chunk()
    doc = chunk_to_doc(c, [0.25, 0.5, 0.75])
    assert doc["embedding"] == [0.25, 0.5, 0.75] and doc["embedding_dim"] == 3
    assert doc["embedding_model"] == "gemini-embedding-2"
    assert doc_to_chunk(doc) == c


# ---------------------------------------------------------------- OpenSearchStore (가짜 클라이언트)


class StubClient:
    """opensearch-py 클라이언트처럼 보이는 가짜. 받은 요청을 `calls`에 기록하고 준비된 응답을 돌려준다."""

    def __init__(self, *, index_exists=False, search_response=None, bulk_response=None,
                 delete_response=None):
        self.calls: list[tuple[str, dict]] = []
        self._search_response = search_response or {"hits": {"hits": []}}
        self._bulk_response = bulk_response or {"errors": False, "items": []}
        self._delete_response = delete_response or {"deleted": 0}
        self.indices = SimpleNamespace(exists=self._exists(index_exists), create=self._record("create"))
        self.search_pipeline = SimpleNamespace(put=self._record("put_pipeline"))

    def _record(self, name):
        def fn(**kwargs):
            self.calls.append((name, kwargs))
            return {"acknowledged": True}
        return fn

    def _exists(self, value):
        def fn(**kwargs):
            self.calls.append(("exists", kwargs))
            return value
        return fn

    def search(self, **kwargs):
        self.calls.append(("search", kwargs))
        return self._search_response

    def bulk(self, **kwargs):
        self.calls.append(("bulk", kwargs))
        return self._bulk_response

    def delete_by_query(self, **kwargs):
        self.calls.append(("delete_by_query", kwargs))
        return self._delete_response

    def names(self):
        return [n for n, _ in self.calls]

    def last(self, name):
        return next(kw for n, kw in reversed(self.calls) if n == name)


# 색인이 없으면 매핑대로 만들고, RRF 파이프라인도 함께 등록한다.
def test_ensure_index_creates_index_and_rrf_pipeline():
    stub = StubClient(index_exists=False)
    OpenSearchStore(stub, "idx").ensure_index(768)
    assert stub.last("create") == {"index": "idx", "body": build_index_body(768)}
    assert stub.last("put_pipeline") == {"id": "rrf-pipeline", "body": build_rrf_pipeline_body()}


# 색인이 이미 있으면 다시 만들지 않는다(데이터 보존). 파이프라인 등록은 덮어쓰기라 매번 해도 안전하다.
def test_ensure_index_keeps_existing_index():
    stub = StubClient(index_exists=True)
    OpenSearchStore(stub, "idx").ensure_index(768)
    assert "create" not in stub.names() and "put_pipeline" in stub.names()


# 검색: 파이프라인 이름을 URL 파라미터로 넘기고, 응답 순서대로 1등부터 순위를 매기며 벡터는 청크에 싣지 않는다.
def test_search_passes_pipeline_and_maps_hits_with_rank():
    a, b = sample_chunk(), sample_chunk(chunk_id="law:근로기준법:436:20250223:56:0", article_no="56")
    resp = {"hits": {"hits": [
        {"_id": a.chunk_id, "_score": 0.031, "_source": chunk_to_doc(a, V)},
        {"_id": b.chunk_id, "_score": 0.016, "_source": chunk_to_doc(b, V)},
    ]}}
    stub = StubClient(search_response=resp)
    hits = OpenSearchStore(stub, "idx").search("제55조", V, size=5)
    call = stub.last("search")
    assert call["index"] == "idx" and call["params"] == {"search_pipeline": "rrf-pipeline"}
    assert call["body"]["size"] == 5
    assert [(h.rank, h.score, h.chunk.article_no) for h in hits] == [(1, 0.031, "55"), (2, 0.016, "56")]
    assert hits[0].chunk == a


# 깊이는 생성자 인자로 받는다(포트는 그대로 두기 위한 판단: 원장 pre-flight 판정).
def test_search_uses_constructor_depth_and_only_current_flag():
    stub = StubClient()
    OpenSearchStore(stub, "idx", depth=77).search("x", V, only_current=False)
    h = stub.last("search")["body"]["query"]["hybrid"]
    assert h["pagination_depth"] == 77 and "filter" not in h


# 색인: 문서 ID는 chunk_id라서 같은 청크를 다시 넣으면 덮어쓴다.
def test_bulk_upsert_uses_chunk_id_as_document_id():
    c = sample_chunk()
    stub = StubClient()
    assert OpenSearchStore(stub, "idx").bulk_upsert([c], [V]) == 1
    body = stub.last("bulk")["body"]
    assert body[0] == {"index": {"_index": "idx", "_id": c.chunk_id}}
    assert body[1]["embedding"] == V and body[1]["article_no"] == "55"


# 청크와 벡터의 개수가 다르면 조용히 잘리지 않고 오류를 낸다.
def test_bulk_upsert_rejects_length_mismatch():
    with pytest.raises(ValueError):
        OpenSearchStore(StubClient(), "idx").bulk_upsert([sample_chunk()], [])


# 일부 문서가 거부되면(errors=true) 성공으로 속이지 않고 예외로 알린다.
def test_bulk_upsert_raises_when_server_reports_errors():
    stub = StubClient(bulk_response={"errors": True, "items": [{"index": {"error": {"type": "x"}}}]})
    with pytest.raises(ExternalServiceError):
        OpenSearchStore(stub, "idx").bulk_upsert([sample_chunk()], [V])


# 지울 원문이 없으면 서버를 부르지 않는다(빈 terms 쿼리가 무엇과 맞는지 따질 필요도 없게).
def test_delete_by_source_with_empty_set_makes_no_call():
    stub = StubClient()
    assert OpenSearchStore(stub, "idx").delete_by_source(set()) == 0
    assert stub.calls == []


def test_delete_by_source_returns_deleted_count():
    stub = StubClient(delete_response={"deleted": 3})
    assert OpenSearchStore(stub, "idx").delete_by_source({"s2", "s1"}) == 3
    assert stub.last("delete_by_query")["body"] == {"query": {"terms": {"source_id": ["s1", "s2"]}}}


# 지금 색인에 있는 원문 ID는 source_id keyword의 집계(terms aggregation)로 읽는다.
def test_list_source_ids_reads_terms_aggregation():
    resp = {"aggregations": {"sources": {"buckets": [{"key": "a", "doc_count": 3}, {"key": "b", "doc_count": 1}]}}}
    assert OpenSearchStore(StubClient(search_response=resp), "idx").list_source_ids() == {"a", "b"}


def test_store_satisfies_ports():
    store = OpenSearchStore(StubClient(), "idx")
    assert isinstance(store, IndexPort) and isinstance(store, SearchPort)
