"""색인 파이프라인: 청크를 임베딩해서 검색 색인에 넣는다.

역할: 파서가 만든 청크 목록을 받아 (1) 제외 대상을 거르고 (2) 배치마다 임베딩 → 색인에 넣고
(3) 이번 입력에 없는 원문의 청크를 색인에서 지운다.
사용처: scripts/build_index.py (서버 색인), 법령이 개정되어 다시 색인할 때.
입력: Chunk 목록, EmbedderPort, IndexPort. 출력: IndexReport(색인한 수, 제외한 수, 지운 수).

핵심 성질 두 가지
- 멱등(idempotent): 같은 입력으로 몇 번을 돌려도 결과가 같다. 문서 ID가 chunk_id라서 같은 청크는 덮어쓴다.
- 이어서 하기: 배치마다 바로 색인에 넣으므로, 무료 한도에 걸려 중간에 멈춰도 앞 배치는 남고 다시 돌리면 채워진다.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Collection, Iterable

from app.domain.models import Chunk, IndexReport
from app.ports import EmbedderPort, IndexPort


def embedding_text(chunk: Chunk) -> str:
    """임베딩에 넣을 글: 제목(법령명 + 조문)과 본문을 함께 담는다.

    본문(`text`)은 "제55조(휴일) …"로 시작해서 어느 법령인지 모른다. 근로기준법 제55조와 같은 번호의
    시행령 조문이 구별되도록 법령명이 든 제목을 앞에 붙인다. 이 선택이 검색 품질에 도움이 되는지는
    평가셋(Task 9)이 생기면 잰다(원장 판정).
    """
    return f"{chunk.title}\n{chunk.text}"


def _ensure_unique_chunk_ids(chunks: list[Chunk]) -> None:
    """같은 chunk_id가 둘이면 색인에서 한쪽이 몰래 덮어써지므로, 시작하기 전에 오류로 알린다."""
    seen: set[str] = set()
    for chunk in chunks:
        if chunk.chunk_id in seen:
            raise ValueError(f"chunk_id가 겹칩니다: {chunk.chunk_id}")
        seen.add(chunk.chunk_id)


def build_index(
    chunks: Iterable[Chunk],
    embedder: EmbedderPort,
    index: IndexPort,
    *,
    exclude_ids: Collection[str] = frozenset(),
    batch_size: int = 50,
    dim: int = 768,
    model: str = "gemini-embedding-2",
) -> IndexReport:
    """청크를 임베딩해 색인에 넣고, 이번 입력에서 사라진 원문의 청크는 색인에서 지운다.

    exclude_ids: 색인하지 않을 chunk_id 또는 source_id(평가용 홀드아웃). 개정 1로 v1에서는 쓰지 않지만,
        계획서의 시그니처를 지키려고 남긴다(빈 값이 기본).
    dim·model: 색인 차원과 임베딩 모델 이름. 청크의 `extra`에 기록해서 나중에 "어느 모델로 만든 색인인지"
        알 수 있게 한다(모델을 바꾸면 전체를 다시 색인해야 하므로).
    """
    all_chunks = list(chunks)
    _ensure_unique_chunk_ids(all_chunks)

    excluded = set(exclude_ids)
    kept = [c for c in all_chunks if c.chunk_id not in excluded and c.source_id not in excluded]
    skipped = len(all_chunks) - len(kept)

    # 색인할 것이 하나도 없으면 멈춘다. 그대로 두면 아래 "입력에 없는 원문은 지운다"가 색인 전체를 지워 버린다
    # (예: 법령 폴더 경로가 틀려 청크가 0개인 경우).
    if not kept:
        raise ValueError("색인할 청크가 없습니다(입력이 비었거나 모두 제외되었습니다). 색인을 건드리지 않았습니다")

    # 색인(과 그 서버)이 준비되었는지 임베딩보다 먼저 확인한다. 안 되면 무료 임베딩 한도를 쓰기 전에 실패한다.
    index.ensure_index(dim)

    indexed = 0
    for start in range(0, len(kept), batch_size):
        batch = kept[start : start + batch_size]
        vectors = embedder.embed_documents([embedding_text(c) for c in batch])
        if len(vectors) != len(batch):
            raise ValueError(f"청크 {len(batch)}개에 벡터 {len(vectors)}개가 돌아왔습니다")
        for vector in vectors:
            if len(vector) != dim:
                raise ValueError(f"벡터 차원이 {len(vector)}입니다(기대 {dim})")
        stamped = [
            dataclasses.replace(c, extra={**c.extra, "embedding_model": model, "embedding_dim": dim})
            for c in batch
        ]
        indexed += index.bulk_upsert(stamped, vectors)

    # 삭제는 모든 배치가 성공한 뒤에만 한다. 도중에 멈춘 실행이 옛 버전을 지워 버리면
    # 새 버전은 덜 채워진 채 옛 버전도 없는 상태가 되기 때문이다.
    stale = index.list_source_ids() - {c.source_id for c in kept}
    deleted = index.delete_by_source(stale) if stale else 0
    return IndexReport(indexed=indexed, skipped_holdout=skipped, deleted=deleted)
