"""검색 평가 지표: Recall@k, MRR, 그리고 검색 결과를 정답 키로 바꾸는 도우미.

역할: "질문에 맞는 조문을 검색이 얼마나 위에 올려 놓았는가"를 숫자로 만든다. 모두 순수 함수라서 외부 서비스 없이 시험된다.
사용처: eval/run_retrieval.py(Task 9), Task 17의 전체 평가 러너가 같은 함수를 이어서 쓴다.

정답은 청크 ID가 아니라 (법령명, 조문번호) 키다(스펙 6.8). 청킹 기준을 바꿔 청크를 다시 잘라도 정답이 그대로 유효해야
"청킹을 바꿨더니 점수가 올랐다"를 공정하게 비교할 수 있기 때문이다.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from app.domain.models import GoldKey, Hit
from eval.evalset import LABELS_WITHOUT_GOLD


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


# --- 답변 단계 지표 (Task 17) ---------------------------------------------------
#
# 위의 검색 지표는 "찾았는가"를 재고, 아래는 서비스가 **끝까지 돌린 결과**(답변 또는 거절)를 잰다.
# 질문 하나의 결과를 `ItemResult`로 모은 뒤, 지표 함수들이 그 목록을 받아 비율을 낸다. 모두 순수 함수다.


@dataclass(frozen=True)
class AnswerSentence:
    """나간 답변의 문장 하나. 사람이 검증기를 채점할 때 (문장, 인용 문서, 검증기 판정)을 한 줄로 보려고 모은다.

    verifier_ok: LLM 검증기가 이 문장을 "인용 문서로 뒷받침됨"으로 봤는가. 판정을 알 수 없으면 None.
    """

    text: str
    citation_ids: tuple[str, ...]
    verifier_ok: bool | None


@dataclass(frozen=True)
class ItemResult:
    """평가 질문 하나를 서비스(그래프)에 끝까지 돌린 결과.

    item_id·label·gold: 평가셋에서 온 값(채점 기준). refused: 거절했는가. cited: 답변이 인용한 출처의 (법령명, 조문번호).
    llm_calls: 이 질문이 쓴 LLM 호출 수. latency_s: 걸린 시간(초, 일부러 쉰 시간은 뺀 값).
    used_reranker: 실제로 리랭커를 썼는가(한도·오류면 False). reranked_count: 리랭크 단계가 남긴 문서 수 —
    0이면 검색·리랭크까지 가지 못한 질문(범위 밖 등)이라 "폴백"으로 세면 안 된다.
    verdict_found: 마지막 검증이 사건 판단 문장을 찾았는가. regen_count·research_count: 다시 쓴 횟수·다시 찾은 횟수.
    question·sentences·sources: 검증기 라벨 시트에 쓴다(sources는 문서 ID → "출처 이름: 본문").
    """

    item_id: str
    label: str
    gold: frozenset[GoldKey]
    refused: bool
    cited: frozenset[GoldKey]
    llm_calls: int
    latency_s: float
    used_reranker: bool
    verdict_found: bool
    regen_count: int = 0
    research_count: int = 0
    reranked_count: int = 0
    question: str = ""
    sentences: tuple[AnswerSentence, ...] = ()
    sources: dict[str, str] = field(default_factory=dict)


def _share(numerator: int, denominator: int, what: str) -> float:
    """비율. 분모가 0이면 0점으로 속이지 않고 무엇이 없는지 알리며 오류를 낸다(위 `_check`와 같은 이유)."""
    if denominator == 0:
        raise ValueError(f"{what}이(가) 하나도 없어서 비율을 낼 수 없습니다")
    return numerator / denominator


def citation_hit_rate(results: Sequence[ItemResult]) -> float:
    """인용 정확도(스펙 6.8의 "인용 정확도"를 구체화한 제안): 답할 수 있는 질문 중 인용한 출처에 정답 조문이 하나라도 있는 비율.

    정답이 여러 개여도 하나만 인용하면 맞은 것이다(사용자가 근거로 삼을 조문을 하나라도 받았다는 뜻).
    답할 수 있는 질문을 거절한 경우는 인용이 없으므로 **틀린 것**으로 센다 — 거절로 점수를 피할 수 없게 한다.
    """
    scored = [r for r in results if r.label == "answerable" and r.gold]
    return _share(sum(bool(r.gold & r.cited) for r in scored), len(scored), "정답이 있는 answerable 질문")


def answerable_refusal_rate(results: Sequence[ItemResult]) -> float:
    """답할 수 있는 질문을 잘못 거절한 비율. 사용자가 가장 먼저 체감하는 실패라서 인용 정확도와 따로 센다."""
    answerable = [r for r in results if r.label == "answerable"]
    return _share(sum(r.refused for r in answerable), len(answerable), "answerable 질문")


def refusal_accuracy(results: Sequence[ItemResult]) -> float:
    """거절해야 하는 질문(라벨 no_evidence·out_of_scope) 중 실제로 거절한 비율."""
    negatives = [r for r in results if r.label in LABELS_WITHOUT_GOLD]
    return _share(sum(r.refused for r in negatives), len(negatives), "거절 대상(no_evidence·out_of_scope) 질문")


def no_verdict_ratio(results: Sequence[ItemResult]) -> float:
    """나간 **답변** 중 사건 판단(결론) 문장이 없다고 검증기가 본 비율. 거절은 답변이 아니라서 분모에 넣지 않는다.

    주의: 서비스는 검증기가 결론 문장을 찾은 답변을 내보내지 않고 다시 쓰거나 거절한다(nodes_verify.decide). 그래서 이 값은
    구조상 거의 항상 1.0에 가깝다. 의미 있는 확인은 사람이 답변을 직접 읽어 보는 표본 확인(스펙 6.8)이고, 다시 쓴 비율
    (`regen_rate`)이 검증기가 실제로 걸러낸 정도를 보여 준다.
    """
    answered = [r for r in results if not r.refused]
    return _share(sum(not r.verdict_found for r in answered), len(answered), "답변(거절이 아닌 결과)")


def validator_accuracy(predicted: Sequence[bool], human: Sequence[bool]) -> float:
    """검증기(LLM)의 문장별 판정이 사람의 판정과 같은 비율. 두 목록은 같은 문장들을 같은 순서로 담아야 한다."""
    if len(predicted) != len(human):
        raise ValueError(f"검증기 판정({len(predicted)}개)과 사람 판정({len(human)}개)의 개수가 다릅니다")
    return _share(sum(p == h for p, h in zip(predicted, human)), len(human), "사람이 채점한 문장")


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """정렬된 값에서 q(0~1) 백분위. 이웃한 두 값 사이를 직선으로 이어 구한다(numpy 기본 방식)."""
    position = q * (len(sorted_values) - 1)
    low = math.floor(position)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def latency_percentiles(values: Sequence[float]) -> tuple[float, float]:
    """(p50, p95): 절반의 질문이 이 시간 안에, 95%의 질문이 이 시간 안에 끝났다는 뜻이다."""
    if not values:
        raise ValueError("지연 시간 값이 없습니다")
    ordered = sorted(values)
    return _percentile(ordered, 0.5), _percentile(ordered, 0.95)


def compute_metrics(results: Sequence[ItemResult]) -> dict[str, float | int | None]:
    """결과 목록에서 리포트에 쓸 지표를 한 번에 모은다. 분모가 0이라 못 재는 지표는 0이 아니라 None(측정 불가)이다."""

    def optional(compute, *args) -> float | None:
        """지표 함수를 부르되, 분모가 0이라 ValueError가 나면 0점 대신 None(측정 불가)을 돌려준다."""
        try:
            return compute(*args)
        except ValueError:
            return None

    reached_rerank = [r for r in results if r.reranked_count > 0]
    p50, p95 = latency_percentiles([r.latency_s for r in results]) if results else (None, None)
    n = len(results)
    return {
        "n_items": n,
        "citation_hit_rate": optional(citation_hit_rate, results),
        "answerable_refusal_rate": optional(answerable_refusal_rate, results),
        "refusal_accuracy": optional(refusal_accuracy, results),
        "no_verdict_ratio": optional(no_verdict_ratio, results),
        "llm_calls_mean": sum(r.llm_calls for r in results) / n if n else None,
        "llm_calls_max": max((r.llm_calls for r in results), default=None),
        "latency_p50_s": p50,
        "latency_p95_s": p95,
        "rerank_fallback_rate": (sum(not r.used_reranker for r in reached_rerank) / len(reached_rerank)) if reached_rerank else None,
        "regen_rate": sum(r.regen_count > 0 for r in results) / n if n else None,
        "research_rate": sum(r.research_count > 0 for r in results) / n if n else None,
    }
