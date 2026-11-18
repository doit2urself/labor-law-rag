"""그래프 실행기: 질문 한 턴을 돌리고, 화면(API)으로 보낼 이벤트만 걸러서 내보낸다.

역할: `run_chat`이 그래프를 `stream_mode="custom"`으로 실행한다. 이 모드에서는 노드가 직접 `get_stream_writer()`로
쓴 값(Progress, StageDetail, AnswerDelta, Final)만 흘러나온다. 상태 전체(values)나 모델 토큰(messages)은 흘러나오지 않는다.
사용처: API(Task 14)가 SSE로 중계하고, 평가 러너(Task 17)가 Final을 읽는다.

핵심 개념 — 왜 custom 스트림만 쓰나(스펙 6.4):
`messages`·`values`·`updates` 모드를 그대로 클라이언트에 보내면, 검증을 통과하기 전의 **초안 답변**이 중간에 화면으로
샌다. 사용자에게 나가야 하는 것은 (1) "지금 이 단계" 진행 상태와 (2) 검증을 통과한 최종 답변뿐이다. 그래서 노드가
의도적으로 쓴 이벤트만 내보내고, 최종 답변(Final)은 통과·거절이 확정된 노드(build.py의 finalize/refuse)만 쓴다.

이 파일에는 이벤트 모양(Progress, Final)과 최종 답변의 화면용 값(payload)을 만드는 함수도 있다. Final.payload의 키는
API(Task 14)·화면(Task 15)과의 약속이다.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from app.graph.nodes_answer import VERDICT_PREFACE, build_context, source_label
from app.graph.state import GraphState, reset_turn_update
from app.texts import DISCLAIMER, SOURCE_NOTICE

# 재귀 한도는 재시도 횟수를 제한하는 장치가 아니라 "그래프가 어떤 버그로 끝없이 도는 것"을 막는 안전망이다.
# 실제 재시도 한도는 regen_count·research_count·llm_calls가 정한다. LangGraph 기본값은 10,007이라 너무 높다(스펙 6.4).
RECURSION_LIMIT = 25


Stage = Literal["analyze", "search", "rerank", "sufficiency", "generate", "verify"]


@dataclass(frozen=True)
class Progress:
    """진행 상태 이벤트: 어느 단계가 **시작**됐는지. 답변 내용은 담지 않는다."""

    stage: Stage
    label: str


@dataclass(frozen=True)
class StageDetail:
    """단계가 **끝난 뒤**의 한 줄 요약(예: 찾은 조문 이름, "충분함"). 화면은 그 단계의 가장 최근 줄 밑에 붙인다.

    Progress와 따로 둔 이유: 요약(찾은 조문 등)은 단계가 끝나야 알 수 있는데 Progress는 시작할 때 나가기 때문이다.
    답변 문장은 담지 않는다(조문 이름·판정 같은 짧은 요약만).
    """

    stage: Stage
    text: str


@dataclass(frozen=True)
class AnswerDelta:
    """화면에 보일 텍스트의 한 조각: **검증을 통과한** 답변(또는 확정된 거절 문구)을 순서대로 흘려보낸다.

    part: "preface"(사건 판단 안내 서두 또는 거절 문구, index 0) 또는 "sentence"(답변 문장, index는 0부터).
    이 이벤트는 통과·거절이 확정된 노드(finalize, refuse)만 낸다. 생성 중인 LLM 토큰을 그대로 흘려보내는 방식이 아니다 —
    그 방식은 검증에서 떨어질 초안이 화면에 새므로(스펙 6.4) 쓰지 않는다. 조각을 이어 붙이면 Final의 텍스트와 같다.
    """

    part: Literal["preface", "sentence"]
    index: int
    text: str


@dataclass(frozen=True)
class Final:
    """최종 이벤트: 화면에 보여 줄 답변 또는 거절. 질문 한 턴에 정확히 한 번 나온다(AnswerDelta들 뒤에)."""

    payload: dict


def answer_deltas(payload: dict) -> list[AnswerDelta]:
    """최종 payload의 텍스트(서두, 문장들)를 스트리밍 조각으로 나눈다. 순서는 화면에 보이는 순서와 같다."""
    deltas = [AnswerDelta("preface", 0, payload["preface"])] if payload["preface"] else []
    deltas += [AnswerDelta("sentence", i, s["text"]) for i, s in enumerate(payload["sentences"])]
    return deltas


def run_config(thread_id: str) -> dict:
    """그래프 호출 설정. thread_id는 대화 하나를 가리키는 키(체크포인트가 이 키로 이력을 저장한다)."""
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}


def run_chat(
    graph: Any, thread_id: str, question: str, config_extra: dict | None = None
) -> Iterator[Progress | StageDetail | AnswerDelta | Final]:
    """질문 한 턴을 실행하며 Progress·StageDetail 이벤트, 통과한 텍스트(AnswerDelta), 마지막 Final 하나를 차례로 내보낸다.

    새 턴은 `reset_turn_update(question)`을 **일반 dict**로 넣는다(지난 턴의 카운터·검색 결과를 비운다).
    한도 초과·외부 서비스 오류는 잡지 않고 호출한 쪽(API)으로 올린다 — 사용자에게 보일 문구와 사용 횟수 차감 여부는 API가 정한다.
    Final 없이 그래프가 끝나면 조용히 넘어가지 않고 RuntimeError를 낸다(조립 실수를 바로 드러낸다).

    config_extra: 호출 설정에 더할 값(예: Langfuse 콜백, 실행 이름, 메타데이터). **우리 설정이 이긴다** — 대화 번호
    (`configurable`)와 재귀 한도(`recursion_limit`)는 이 인자로 바꿀 수 없다(다른 대화 번호로 새거나 안전망이 풀리는 일을 막는다).
    """
    got_final = False
    config = {**(config_extra or {}), **run_config(thread_id)}
    for event in graph.stream(reset_turn_update(question), config, stream_mode="custom"):
        if isinstance(event, Final):
            got_final = True
        yield event
    if not got_final:
        raise RuntimeError("그래프가 최종 답변(Final) 없이 끝났습니다")


def _source_entry(doc_id: str, hit) -> dict:
    """출처 한 건. 화면이 바로 쓸 라벨·주소·원문 일부와, 조문번호/가지번호를 나눈 값(formatSource 계약)을 함께 담는다."""
    chunk = hit.chunk
    article_no, _, branch_no = (chunk.article_no or "").partition("의")  # "43의2" → ("43", "2")
    return {
        "id": doc_id,
        "label": source_label(chunk),
        "doc_type": chunk.doc_type,
        "law_name": chunk.law_name,
        "article_no": article_no or None,
        "branch_no": branch_no or None,
        "url": chunk.source_url,
        "text": chunk.text,
    }


def answer_payload(state: GraphState) -> dict:
    """통과한 답변의 화면용 값: 문장(인용 ID 포함), 실제로 인용된 출처, 고정 안내 문구.

    - sources: 인용된 문서만, 처음 인용된 순서로, 중복 없이. (문서 ID는 build_context가 `reranked` 순서로 정한 것과 같다.)
    - 사건 판단 요청(verdict_request)이면 서두 문장을 `preface`로 올린다. 모델이 첫 문장에 같은 문장을 썼다면 중복되지
      않도록 뺀다. 모델이 서두를 빠뜨려도 preface는 항상 나간다 — 정책 문구를 모델의 기분에 맡기지 않는다.
    """
    _, id_map = build_context(state.get("reranked", []))
    answer = state.get("answer")
    sentences = list(answer.sentences) if answer else []

    preface = None
    if state.get("qclass") == "verdict_request":
        preface = VERDICT_PREFACE
        if sentences and sentences[0].kind == "general" and sentences[0].text.strip() == VERDICT_PREFACE:
            sentences = sentences[1:]

    sources: list[dict] = []
    seen: set[str] = set()
    for sentence in sentences:
        for doc_id in sentence.citation_ids:
            if doc_id in id_map and doc_id not in seen:
                seen.add(doc_id)
                sources.append(_source_entry(doc_id, id_map[doc_id]))

    return {
        "refused": False,
        "preface": preface,
        "sentences": [{"text": s.text, "citation_ids": list(s.citation_ids), "kind": s.kind} for s in sentences],
        "sources": sources,
        "notice": [DISCLAIMER, SOURCE_NOTICE],
    }


def refusal_payload(message: str) -> dict:
    """거절의 화면용 값. 안내 문구를 `preface`에 담고 답변 문장·출처는 비운다."""
    return {"refused": True, "preface": message, "sentences": [], "sources": [], "notice": [DISCLAIMER, SOURCE_NOTICE]}
