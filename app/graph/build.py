"""그래프 조립: 노드들을 이어 붙여 하나의 실행 가능한 그래프(LangGraph)로 만든다.

역할: Task 10~13에서 만든 노드와 갈림길 함수를 스펙 6.4의 순서로 연결한다.
사용처: 서비스 조립(container.py, Task 14)과 테스트·평가 러너(Task 17).

흐름(스펙 6.4):

    analyze ─┬─(근로 상담·사건 판단)→ retrieve → rerank → sufficiency ─┬─(충분)→ generate → check ─┐
             │                                      ▲                  ├─(부족)→ refine ─┐         │
             └─(범위 밖·수집 범위 밖)→ refuse        └──────────────────┼─────────────────┘         │
                                                                       └─(예산·한도 소진)→ refuse   │
    check ─┬─(코드 검사 위반)→ decide ─┐                                                            │
           └─(위반 없음)→ verify ──────┴→ decide ─┬─ pass → finalize                               │
                                                  ├─ regenerate → count_regen → generate           │
                                                  ├─ research → refine → (새 검색어면) retrieve     │
                                                  └─ refuse → refuse                               │

핵심 개념 — 진행 이벤트를 노드 안이 아니라 "감싸는 함수"에서 낸다:
노드 함수(Task 10~12)는 LangGraph 밖에서도 단독으로 시험할 수 있어야 하므로 스트림 쓰기 도구를 모른다. 이 파일의
`_step`이 노드를 감싸서, 노드가 시작될 때 Progress를 쓴다. 최종 답변(Final)은 통과·거절이 확정된 두 노드(finalize,
refuse)만 쓴다. 이 두 노드는 확정된 텍스트를 문장 단위 조각(AnswerDelta)으로 먼저 흘려보낸 뒤 Final을 쓴다 — 그래서 화면에는 글이
점점 나타나지만 검증 전 초안은 새지 않는다. 노드가 끝난 뒤의 짧은 요약(StageDetail: 찾은 조문 이름, 충분함/부족함)도 이 감싸는 함수가 낸다.
클라이언트에는 이 이벤트들만 나간다(runner.py).
"""

from collections.abc import Callable

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from app.graph.budget import ANSWER_CALLS, has_room
from app.graph.deps import GraphDeps
from app.graph.nodes_analyze import analyze_node
from app.graph.nodes_answer import check_node, generate_node
from app.graph.nodes_search import (
    refine_node,
    rerank_node,
    retrieve_node,
    route_after_analyze,
    route_after_refine,
    route_after_sufficiency,
    sufficiency_node,
)
from app.graph.nodes_verify import RefusalKind, decide, refusal_message, verify_node
from app.graph.runner import Final, Progress, StageDetail, answer_deltas, answer_payload, refusal_payload
from app.graph.state import GraphState
from app.ingest.parse_law import article_label


def _step(
    stage: str,
    label: str | Callable[[GraphState], str],
    node: Callable[[GraphState], dict],
    detail: Callable[[GraphState, dict], str] | None = None,
):
    """노드를 감싸서 (1) 시작할 때 진행 이벤트(Progress)를, (2) 끝난 뒤 요약 이벤트(StageDetail)를 쓴다.

    label이 함수면 상태를 보고 문구를 고른다(예: 재검색일 때는 "다시 찾는 중 (1/2)").
    detail은 (시작 때의 상태, 노드가 돌려준 갱신)을 받아 한 줄 요약을 만든다. 요약은 짧은 조문 이름·판정만 담고
    답변 문장은 담지 않는다.
    """

    def run(state: GraphState) -> dict:
        writer = get_stream_writer()
        writer(Progress(stage, label(state) if callable(label) else label))  # type: ignore[arg-type]
        update = node(state)
        if detail is not None:
            writer(StageDetail(stage, detail(state, update)))  # type: ignore[arg-type]
        return update

    return run


def _found_articles(_state: GraphState, update: dict) -> str:
    """검색 단계 요약: 찾은 조문 앞의 3개를 이름으로 보이고 더 있으면 "…"를 붙인다. 같은 법령명은 한 번만 쓴다.

    예: "근로기준법 제55조, 제56조, 제50조 …". 후보 20개 전체가 아니라 앞의 3개만 보여 주는 것은 한 줄로 읽히게 하려는 것이다.
    """
    hits = update.get("candidates", [])
    if not hits:
        return "찾은 조문 없음"
    parts: list[str] = []
    previous_law = None
    for hit in hits[:3]:
        chunk = hit.chunk
        name = article_label(chunk.article_no) if chunk.article_no else chunk.title
        if chunk.law_name and chunk.law_name != previous_law:
            name = f"{chunk.law_name} {name}"
        parts.append(name)
        previous_law = chunk.law_name
    return ", ".join(parts) + (" …" if len(hits) > 3 else "")


def _sufficiency_summary(_state: GraphState, update: dict) -> str:
    """근거 확인 단계 요약: "충분함" 또는 "부족함"."""
    return "충분함" if update.get("sufficient") else "부족함"


def _history_update(state: GraphState, assistant_text: str) -> dict:
    """이번 턴이 끝났을 때 대화 이력에 (사용자 질문, 상담사 답)을 덧붙인다.

    질문 분석은 "이전에 끝난 턴들"을 이력으로 읽으므로(nodes_analyze.format_history), 이력에 넣는 일은 턴이 확정된
    마지막 노드가 한다. 답변은 검증을 통과한 문장(또는 거절 문구)만 넣는다.
    """
    return {"messages": [("user", state["original_question"]), ("assistant", assistant_text)]}


def build_graph(deps: GraphDeps, checkpointer):
    """노드를 연결해 컴파일된 그래프를 돌려준다. checkpointer는 대화 상태를 저장하는 곳(운영: SQLite, 테스트: 메모리)."""
    settings = deps.settings
    covered_laws = deps.covered_laws

    def _emit_final(payload: dict) -> None:
        """확정된 텍스트를 조각(AnswerDelta)으로 먼저 흘려보내고, 마지막에 Final을 보낸다.

        조각은 통과·거절이 확정된 여기서만 나가므로 검증 전 초안은 어디로도 새지 않는다.
        """
        writer = get_stream_writer()
        for delta in answer_deltas(payload):
            writer(delta)
        writer(Final(payload))

    def count_regen(state: GraphState) -> dict:
        """다시 쓰기로 정했을 때 횟수를 1 올린다. (갈림길 함수는 상태를 바꿀 수 없어서 별도 노드가 한다.)"""
        return {"regen_count": state.get("regen_count", 0) + 1}

    def finalize(state: GraphState) -> dict:
        """검증을 통과한 답변을 최종 이벤트로 내보내고 이력에 남긴다."""
        payload = answer_payload(state)
        _emit_final(payload)
        spoken = " ".join(filter(None, [payload["preface"], *(s["text"] for s in payload["sentences"])]))
        return _history_update(state, spoken)

    def refuse(state: GraphState) -> dict:
        """거절 안내를 최종 이벤트로 내보내고 이력에 남긴다. 거절 종류는 질문 분류와 남은 예산으로 정한다."""
        kind = _refusal_kind(state)
        message = refusal_message(kind, covered_laws)
        _emit_final(refusal_payload(message))
        return _history_update(state, message)

    def _refusal_kind(state: GraphState) -> RefusalKind:
        qclass = state.get("qclass")
        if qclass == "out_of_scope":
            return "out_of_scope"
        if qclass == "uncovered_labor":
            return "no_evidence"  # 근로 분야지만 수집 범위 밖 → 근거 없음으로 안내(스펙 6.2)
        if not has_room(state.get("llm_calls", 0), settings, ANSWER_CALLS):
            return "limit"  # 호출 예산이 바닥나서 답변 한 번(생성+검증)을 더 할 수 없다
        return "no_evidence"

    def after_check(state: GraphState) -> str:
        """코드 검사 뒤: 위반이 있으면 LLM 검증을 건너뛰고 바로 갈림길로, 없으면 LLM 검증으로."""
        return decide(state, settings) if state.get("violations") else "verify"

    builder = StateGraph(GraphState)
    builder.add_node("analyze", _step("analyze", "질문을 분석하는 중", analyze_node(deps)))
    builder.add_node(
        "retrieve",
        _step(
            "search",
            lambda s: (
                f"검색어를 바꿔 다시 찾는 중 ({s['research_count']}/{settings.max_research})"
                if s.get("research_count", 0)
                else "법령 조문을 찾는 중"
            ),
            retrieve_node(deps),
            _found_articles,
        ),
    )
    builder.add_node("rerank", _step("rerank", "관련도가 높은 조문을 고르는 중", rerank_node(deps)))
    builder.add_node(
        "sufficiency",
        _step(
            "sufficiency",
            "근거가 충분한지 확인하는 중",
            sufficiency_node(deps, settings.rerank_min_score),
            _sufficiency_summary,
        ),
    )
    builder.add_node("refine", refine_node(deps))  # 검색어만 바꾸는 일이라 따로 진행 이벤트를 내지 않는다(곧 이어지는 retrieve가 낸다)
    builder.add_node(
        "generate",
        _step(
            "generate",
            lambda s: (
                f"답변을 다시 쓰는 중 ({s['regen_count']}/{settings.max_regen})"
                if s.get("regen_count", 0)
                else "답변을 작성하는 중"
            ),
            generate_node(deps),
        ),
    )
    builder.add_node("check", check_node())  # 코드 검사는 순식간이라 진행 이벤트가 없다
    builder.add_node("verify", _step("verify", "답변이 근거와 맞는지 확인하는 중", verify_node(deps)))
    builder.add_node("count_regen", count_regen)
    builder.add_node("finalize", finalize)
    builder.add_node("refuse", refuse)

    builder.add_edge(START, "analyze")
    builder.add_conditional_edges(
        "analyze", route_after_analyze, {"retrieve": "retrieve", "refuse": "refuse"}
    )
    builder.add_edge("retrieve", "rerank")
    builder.add_edge("rerank", "sufficiency")
    builder.add_conditional_edges(
        "sufficiency",
        lambda s: route_after_sufficiency(s, settings),
        {"generate": "generate", "refine": "refine", "refuse": "refuse"},
    )
    builder.add_conditional_edges("refine", route_after_refine, {"retrieve": "retrieve", "refuse": "refuse"})
    builder.add_edge("generate", "check")
    outcomes = {"pass": "finalize", "regenerate": "count_regen", "research": "refine", "refuse": "refuse"}
    builder.add_conditional_edges("check", after_check, {"verify": "verify", **outcomes})
    builder.add_conditional_edges("verify", lambda s: decide(s, settings), outcomes)
    builder.add_edge("count_regen", "generate")
    builder.add_edge("finalize", END)
    builder.add_edge("refuse", END)
    return builder.compile(checkpointer=checkpointer)
