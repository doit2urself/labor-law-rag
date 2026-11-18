"""평가셋: 항목 모델, JSONL 저장·읽기, 규칙 검사.

역할: "이 질문에는 이 조문이 정답이다"를 모아 둔 목록을 다룬다. 검색 품질(Task 9)과 답변 품질(Task 17)을 같은 목록으로 잰다.
사용처: eval/drafts.py(초안 생성), eval/labels.py(사람 검수 CSV), eval/run_retrieval.py(검색 측정), Task 17 러너.

항목 하나의 구성(질문을 두 가지 문장으로 둔 이유):
- `question` = **조문 어휘형**: 조문에 나오는 말을 그대로 쓴 질문("1주 동안 소정근로일을 개근한 근로자에게 유급휴일…").
- `casual_question` = **일상어형**: 사람들이 실제로 쓰는 말("알바 주휴수당이 뭐예요?"). 법령 본문에는 "주휴"가 한 번도 안 나온다.
같은 정답에 두 문장으로 검색해 점수를 비교하면, "조문에서 질문을 만들면 점수가 부풀려진다"는 한계(스펙 개정 1)가 얼마나 큰지 숫자로 보인다.
조문 어휘형 점수는 상한선에 가깝고, 일상어형 점수가 실제 사용에 더 가깝다.

정답은 청크 ID가 아니라 (법령명, 조문번호) 키다(스펙 6.8). 조문번호는 가지 조문을 "43의2"처럼 한 문자열로 쓴다(Task 4).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

from app.domain.models import GoldKey

Label = Literal["answerable", "verdict", "no_evidence", "out_of_scope"]
LABELS: tuple[str, ...] = get_args(Label)

# 정답 조문이 있어야 하는 라벨 / 없어야 하는 라벨.
# verdict(사건 판단 요청)는 답변은 결론 없이 근거만 보이지만, 근거로 보일 조문은 있으므로 정답이 있다.
LABELS_WITH_GOLD = ("answerable", "verdict")
LABELS_WITHOUT_GOLD = ("no_evidence", "out_of_scope")


@dataclass(frozen=True)
class EvalItem:
    """평가셋 항목 하나.

    id: 항목 고유 번호. a=정답 있는 상담, v=사건 판단 요청, n=근거 없음(근로 분야지만 수집 범위 밖), o=범위 밖.
    gold: 정답 (법령명, 조문번호) 모음. 거절 대상(n, o)은 비어 있다.
    gold_answer: 정답 근거를 사람이 읽고 확인할 수 있게 적은 요약(채점 기준 참고용). 법령 본문에서 읽은 내용만 적는다.
    reviewer: `casual_question`을 사람이 확인했으면 그 사람 이름. None이면 아직 사람이 보지 않은 초안이다.
              (평가 리포트가 "사람이 확인한 건수"를 적는 근거다.)
    """

    id: str
    question: str
    casual_question: str | None
    gold: frozenset[GoldKey]
    gold_answer: str
    label: Label
    reviewer: str | None = None


def article_sort_key(key: GoldKey) -> tuple[str, int, int]:
    """정답 키를 사람이 읽는 순서로 줄 세우는 열쇠: 법령명, 조 번호(숫자), 가지 번호. 문자열 정렬이면 "9"가 "55" 뒤로 간다."""
    law, article = key
    match = re.fullmatch(r"(\d+)(?:의(\d+))?", article)
    if match is None:
        return (law, 10**9, 0)
    return (law, int(match.group(1)), int(match.group(2) or 0))


def _to_json(item: EvalItem) -> str:
    """항목 하나를 JSON 한 줄로 만든다. 정답은 정렬해서 쓰므로 같은 항목은 언제나 같은 줄이 된다."""
    record = {
        "id": item.id,
        "label": item.label,
        "question": item.question,
        "casual_question": item.casual_question,
        "gold": [list(k) for k in sorted(item.gold, key=article_sort_key)],
        "gold_answer": item.gold_answer,
        "reviewer": item.reviewer,
    }
    return json.dumps(record, ensure_ascii=False)


def save_evalset(items: Iterable[EvalItem], path: str | Path) -> None:
    """평가셋을 JSONL(한 줄에 항목 하나)로 저장한다.

    한글을 이스케이프하지 않고(사람이 파일을 열어 읽을 수 있게), 줄바꿈은 LF로 고정한다. Windows에서 기본 방식으로 쓰면
    CRLF가 되어 git diff가 줄마다 바뀌어 보인다(이 프로젝트에서 이미 한 번 겪은 일).
    """
    with open(path, "w", encoding="utf-8", newline="\n") as file:
        for item in items:
            file.write(_to_json(item) + "\n")


def load_evalset(path: str | Path) -> list[EvalItem]:
    """JSONL 평가셋을 읽는다. 모양이 틀리면 몇 번째 줄인지 알려 주며 ValueError를 낸다."""
    items: list[EvalItem] = []
    text = Path(path).read_text(encoding="utf-8")
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            if raw["label"] not in LABELS:
                raise ValueError(f"알 수 없는 라벨 {raw['label']!r}")
            items.append(
                EvalItem(
                    id=raw["id"],
                    question=raw["question"],
                    casual_question=raw["casual_question"],
                    gold=frozenset((law, article) for law, article in raw["gold"]),
                    gold_answer=raw["gold_answer"],
                    label=raw["label"],
                    reviewer=raw.get("reviewer"),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path} {number}번째 줄을 읽을 수 없습니다: {exc}") from exc
    return items


def validate_items(items: list[EvalItem], covered_laws: set[str]) -> list[str]:
    """평가셋이 규칙을 지키는지 검사하고, 어긴 곳을 문장 목록으로 돌려준다(비어 있으면 통과).

    규칙: id 중복 없음 / 질문이 비어 있지 않음 / 정답이 필요한 라벨(answerable, verdict)에는 정답이 있음 /
    거절 대상(no_evidence, out_of_scope)에는 정답이 없음 / 정답 법령이 수집 범위 안(`covered_laws`)임.
    마지막 규칙의 이유: 수집하지 않은 법령이 정답이면 검색이 구조적으로 못 맞혀서 지표가 0이 된다(스펙 6.2).
    """
    problems: list[str] = []
    for item_id, count in Counter(i.id for i in items).items():
        if count > 1:
            problems.append(f"id 중복: {item_id} ({count}번)")
    for item in items:
        if not item.question.strip():
            problems.append(f"{item.id}: 질문이 비어 있습니다")
        if item.label in LABELS_WITH_GOLD and not item.gold:
            problems.append(f"{item.id}: 라벨 {item.label}에는 정답 조문이 있어야 합니다")
        if item.label in LABELS_WITHOUT_GOLD and item.gold:
            problems.append(f"{item.id}: 라벨 {item.label}(거절 대상)에는 정답 조문이 없어야 합니다")
        for law, article in sorted(item.gold, key=article_sort_key):
            if law not in covered_laws:
                problems.append(f"{item.id}: 수집 범위 밖 법령이 정답입니다: {law} 제{article}조")
    return problems


def check_gold_exists(items: list[EvalItem], known: set[GoldKey]) -> list[str]:
    """정답 조문이 실제 색인 대상(파서가 읽은 조문)에 있는지 확인하고, 없는 것을 문장 목록으로 돌려준다.

    known: 파서가 읽은 모든 청크의 (법령명, 조문번호). 없는 조문을 정답으로 적으면 어떤 검색기도 못 맞힌다.
    """
    problems: list[str] = []
    for item in items:
        for law, article in sorted(item.gold, key=article_sort_key):
            if (law, article) not in known:
                problems.append(f"{item.id}: 색인 대상에 없는 조문입니다: {law} 제{article}조")
    return problems


def label_shares(items: list[EvalItem]) -> dict[str, float]:
    """라벨별 비율(분모는 전체 항목 수). 스펙 6.8의 "답변 불가 10~20%, 사건 판단형 약 10%"를 확인하는 데 쓴다."""
    counts = Counter(i.label for i in items)
    total = len(items)
    return {label: (counts[label] / total if total else 0.0) for label in LABELS}
