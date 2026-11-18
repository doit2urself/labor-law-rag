"""평가 리포트: 지표 모음을 마크다운 글로 만든다 (Task 17).

역할: `eval/run_eval.py`가 모은 지표(`compute_metrics`의 결과)를 사람이 읽는 표로 바꾼다. 결과는 docs/results.md에 저장하고
README에 요약한다(스펙 6.8: 지표는 웹 화면이 아니라 리포트로 보여 준다).

두 가지 원칙
- **사람이 확인한 건수를 반드시 적는다.** 평가셋과 검증기 판정을 모델이 만들고 모델이 채점하면 점수가 부풀 수 있다.
  리포트를 읽는 사람이 "이 숫자 중 사람이 직접 본 것이 얼마인가"를 바로 알아야 한다(스펙 6.8).
- **측정하지 못한 지표는 0이 아니라 "측정 불가"로 적는다.** 0점과 "잴 수 없음"은 전혀 다른 말이다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

# 지표 이름 → 리포트에 쓰는 설명. 여기에 없는 이름은 이름 그대로 표시한다(예: 검색 지표 "recall@20").
LABELS = {
    "n_items": "평가한 질문 수",
    "citation_hit_rate": "인용 정확도 (답할 수 있는 질문 중 정답 조문을 인용한 비율)",
    "answerable_refusal_rate": "답할 수 있는 질문을 잘못 거절한 비율",
    "refusal_accuracy": "거절해야 하는 질문을 거절한 비율",
    "no_verdict_ratio": "답변 중 결론 문장이 없는 비율 (검증기 판정)",
    "llm_calls_mean": "질문당 LLM 호출 수 (평균)",
    "llm_calls_max": "질문당 LLM 호출 수 (최대)",
    "latency_p50_s": "지연 시간 p50 (초)",
    "latency_p95_s": "지연 시간 p95 (초)",
    "rerank_fallback_rate": "리랭크 폴백 비율 (리랭크 단계까지 간 질문 중)",
    "regen_rate": "답변을 다시 쓴 질문의 비율",
    "research_rate": "검색을 다시 한 질문의 비율",
    "validator_accuracy": "검증기 정확도 (사람 판정과 같은 문장의 비율)",
    "validator_labels": "검증기를 사람이 채점한 문장 수",
}

UNMEASURED = "측정 불가"


def _format_value(value: object) -> str:
    """값 하나를 표 칸의 글로 만든다. None은 "측정 불가", 실수는 소수 둘째 자리, 그 밖은 있는 그대로."""
    if value is None:
        return UNMEASURED
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def render_report(
    metrics: Mapping[str, object],
    human_checked: int,
    *,
    title: str = "평가 리포트",
    notes: Sequence[str] = (),
) -> str:
    """지표 모음을 마크다운으로 만든다.

    metrics: {지표 이름: 값}. 값이 None이면 "측정 불가"로 적는다.
    human_checked: 사람이 직접 확인한 건수(평가셋 일상어 질문 검수 + 검증기 라벨). 0이어도 0이라고 적는다.
    notes: 읽는 사람이 알아야 할 한계·조건(예: 평가셋이 초안이다, 이 지표는 구조상 높게 나온다). 목록으로 붙인다.
    """
    lines = [f"# {title}", "", f"사람이 확인한 건수: {human_checked}", "", "| 지표 | 값 |", "|---|---|"]
    for name, value in metrics.items():
        lines.append(f"| {LABELS.get(name, name)} | {_format_value(value)} |")
    if notes:
        lines += ["", "## 읽을 때 알아둘 점", ""]
        lines += [f"- {note}" for note in notes]
    return "\n".join(lines) + "\n"
