"""검색 평가 지표: Recall@k, MRR, 그리고 검색 결과를 정답 키로 바꾸는 도우미.

역할: "질문에 맞는 조문을 검색이 얼마나 위에 올려 놓았는가"를 숫자로 만든다. 모두 순수 함수라서 외부 서비스 없이 시험된다.
사용처: eval/run_retrieval.py(Task 9), Task 17의 전체 평가 러너가 같은 함수를 이어서 쓴다.

정답은 청크 ID가 아니라 (법령명, 조문번호) 키다(스펙 6.8). 청킹 기준을 바꿔 청크를 다시 잘라도 정답이 그대로 유효해야
"청킹을 바꿨더니 점수가 올랐다"를 공정하게 비교할 수 있기 때문이다.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.domain.models import GoldKey, Hit


def _check(gold: set[GoldKey] | frozenset[GoldKey]) -> None:
    """정답이 비어 있으면 오류를 낸다.

    정답이 없는 질문(범위 밖·근거 없음)은 "무엇을 찾아야 하는가"가 없으므로 검색 지표를 매길 수 없다. 0점으로 처리하면
    평균이 조용히 깎이므로, 호출하는 쪽이 그런 질문을 먼저 걸러내도록 강제한다.
    """
    if not gold:
        raise ValueError("정답이 비어 있는 질문에는 검색 지표를 매길 수 없습니다")


def recall_at_k(gold: set[GoldKey] | frozenset[GoldKey], ranked: list[GoldKey], k: int) -> float:
    """재현율@k: 정답 조문 중 검색 결과 상위 k개 안에 들어온 비율(0~1).

    gold: 이 질문의 정답 (법령명, 조문번호) 모음. ranked: 검색 결과를 위에서부터 키로 바꾼 목록.
    같은 정답이 목록에 여러 번 나와도 한 번으로 센다(집합 교집합), 그래서 1을 넘지 않는다.
    """
    _check(gold)
    if k < 1:
        raise ValueError("k는 1 이상이어야 합니다")
    return len(gold & set(ranked[:k])) / len(gold)


def mrr(gold: set[GoldKey] | frozenset[GoldKey], ranked: list[GoldKey]) -> float:
    """MRR(평균 역순위)의 한 질문분: 처음 맞힌 정답의 순위 r에 대해 1/r, 하나도 못 맞히면 0.

    정답이 여러 개여도 **처음 맞힌 것**만 본다. "사용자가 맞는 조문을 처음 보게 되는 위치"를 재는 지표이기 때문이다.
    여러 질문의 평균은 호출하는 쪽에서 낸다.
    """
    _check(gold)
    for position, key in enumerate(ranked, start=1):
        if key in gold:
            return 1.0 / position
    return 0.0


def hit_keys(hits: Iterable[Hit]) -> list[GoldKey]:
    """검색 결과(Hit)를 위에서부터 (법령명, 조문번호) 목록으로 바꾼다.

    - 한 조문이 항 단위 등으로 여러 청크가 되어도 **첫 등장 위치 한 번만** 남긴다. 안 그러면 한 조문이 순위 여러 칸을
      차지해서 MRR이 실제보다 나빠 보인다.
    - 법령명이나 조문번호가 없는 청크는 정답 키를 만들 수 없으므로 뺀다.
    """
    seen: set[GoldKey] = set()
    keys: list[GoldKey] = []
    for hit in hits:
        law, article = hit.chunk.law_name, hit.chunk.article_no
        if law is None or article is None:
            continue
        key = (law, article)
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys
