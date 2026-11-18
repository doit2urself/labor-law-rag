"""색인 파이프라인(build_index) 단위 테스트: 가짜 임베더·가짜 색인으로 로직만 시험한다(네트워크 없음)."""

from __future__ import annotations

import pytest

from app.domain.errors import QuotaExceeded
from app.domain.models import Chunk
from app.ingest.indexer import build_index, embedding_text
from tests.conftest import FakeEmbedder, FakeIndex


def chunk(chunk_id: str, *, source_id: str | None = None, text: str = "본문") -> Chunk:
    """테스트용 청크. source_id를 안 주면 chunk_id와 같게 둔다(원문 하나에 청크 하나)."""
    return Chunk(
        chunk_id=chunk_id, doc_type="law", title=f"제목 {chunk_id}", text=text, law_name="근로기준법",
        article_no="1", paragraph=None, effective_date="2025-01-01", promulgation_date="2024-12-01",
        source_id=source_id or chunk_id, source_url=None, extra={},
    )


class RecordingEmbedder(FakeEmbedder):
    """FakeEmbedder에 '받은 글'과 '호출 횟수'를 기록한다. fail_on_call을 주면 그 번째 호출에서 한도 초과를 낸다."""

    def __init__(self, fail_on_call: int | None = None) -> None:
        super().__init__()
        self.batches: list[list[str]] = []
        self._fail_on_call = fail_on_call

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        if self._fail_on_call == len(self.batches):
            raise QuotaExceeded("한도")
        return super().embed_documents(texts)


# 제외 목록(exclude_ids)에 든 청크는 색인하지 않고 세며, 같은 입력으로 다시 돌려도 문서가 늘지 않는다(멱등).
def test_build_index_excludes_ids_and_is_idempotent():
    idx = FakeIndex()
    chunks = [chunk("interp:1"), chunk("interp:2")]
    r1 = build_index(chunks, FakeEmbedder(), idx, exclude_ids={"interp:2"})
    r2 = build_index(chunks, FakeEmbedder(), idx, exclude_ids={"interp:2"})
    assert (r1.indexed, r1.skipped_holdout) == (1, 1) and idx.count() == 1 and r2.indexed == 1


# 이번 입력에 없는 원문의 청크는 색인에서 지우고, 청크에 임베딩 모델명·차원을 기록한다.
def test_build_index_deletes_removed_sources_and_records_model():
    idx = FakeIndex()
    build_index([chunk("a"), chunk("b")], FakeEmbedder(), idx)
    r = build_index([chunk("a")], FakeEmbedder(), idx)
    assert r.deleted == 1 and idx.count() == 1
    assert idx.get("a").extra == {"embedding_model": "gemini-embedding-2", "embedding_dim": 768}


# 제외 목록은 chunk_id뿐 아니라 source_id로도 걸러진다(원문 통째로 빼는 경우).
def test_exclude_matches_source_id_too():
    idx = FakeIndex()
    chunks = [chunk("s1:1", source_id="s1"), chunk("s1:2", source_id="s1"), chunk("s2:1", source_id="s2")]
    r = build_index(chunks, FakeEmbedder(), idx, exclude_ids={"s1"})
    assert (r.indexed, r.skipped_holdout) == (1, 2)


# 같은 chunk_id가 둘이면 색인 단계에서 몰래 덮어써지므로, 시작하기 전에 오류로 알린다.
def test_duplicate_chunk_ids_are_rejected_before_any_call():
    embedder = RecordingEmbedder()
    with pytest.raises(ValueError):
        build_index([chunk("a"), chunk("a")], embedder, FakeIndex())
    assert embedder.batches == []


# 임베딩에 넣는 글은 제목(법령명+조문)과 본문을 함께 담는다: 벡터가 "어느 법령의 조문인지"도 알게 하려는 것이다.
def test_embedding_text_contains_title_and_body():
    c = chunk("a", text="제1조(목적) 이 법은")
    assert embedding_text(c) == "제목 a\n제1조(목적) 이 법은"


def test_embedder_receives_the_embedding_text():
    embedder = RecordingEmbedder()
    build_index([chunk("a", text="본문 하나")], embedder, FakeIndex())
    assert embedder.batches == [["제목 a\n본문 하나"]]


# 배치(50개)마다 임베딩 호출 한 번: 120개면 50, 50, 20.
def test_batches_calls_to_the_embedder():
    embedder = RecordingEmbedder()
    chunks = [chunk(f"c{i}") for i in range(120)]
    r = build_index(chunks, embedder, FakeIndex(), batch_size=50)
    assert [len(b) for b in embedder.batches] == [50, 50, 20] and r.indexed == 120


# 둘째 배치에서 한도 초과가 나도 첫 배치는 이미 색인에 들어가 있고, 예외는 그대로 올라온다(다시 돌리면 이어서 채운다).
# 또 실패한 실행은 "사라진 원문 삭제"를 하지 않는다(새 버전이 덜 채워진 채로 옛 버전을 지우면 안 되므로).
def test_quota_error_keeps_finished_batches_and_skips_deletion():
    idx = FakeIndex()
    build_index([chunk("old")], FakeEmbedder(), idx)  # 옛 원문
    chunks = [chunk(f"c{i}") for i in range(30)]
    with pytest.raises(QuotaExceeded):
        build_index(chunks, RecordingEmbedder(fail_on_call=2), idx, batch_size=10)
    assert idx.count() == 1 + 10 and "old" in idx.list_source_ids()


# 임베더가 설정한 차원과 다른 길이의 벡터를 주면 색인에 넣기 전에 알린다.
def test_wrong_vector_length_is_rejected():
    with pytest.raises(ValueError):
        build_index([chunk("a")], FakeEmbedder(dim=3), FakeIndex())


# 색인은 임베딩을 쓰기 전에 만든다(색인 서버가 안 될 때 무료 한도를 먼저 쓰지 않도록).
def test_index_is_ensured_before_embedding():
    order: list[str] = []

    class Idx(FakeIndex):
        def ensure_index(self, dim: int) -> None:
            order.append(f"ensure({dim})")
            super().ensure_index(dim)

    class Emb(FakeEmbedder):
        def embed_documents(self, texts):
            order.append("embed")
            return super().embed_documents(texts)

    build_index([chunk("a")], Emb(), Idx())
    assert order == ["ensure(768)", "embed"]


# 색인할 청크가 하나도 없으면(예: 법령 폴더 경로 실수) "입력에 없는 원문은 지운다" 규칙이 색인 전체를 지우면 안 된다.
def test_empty_input_is_rejected_and_never_wipes_the_index():
    idx = FakeIndex()
    build_index([chunk("a")], FakeEmbedder(), idx)
    with pytest.raises(ValueError):
        build_index([], FakeEmbedder(), idx)
    with pytest.raises(ValueError):  # 전부 제외 목록에 걸려도 마찬가지
        build_index([chunk("b")], FakeEmbedder(), idx, exclude_ids={"b"})
    assert idx.count() == 1
