"""그래프 조립·실행기(run_chat)·화면용 최종 값(payload) 테스트: 가짜 LLM·검색기·인메모리 체크포인터로 처음부터 끝까지 돌린다.

검증 대상: app/graph/build.py, app/graph/runner.py (노드들의 연결과 스트리밍 이벤트).
"""

from __future__ import annotations

import dataclasses
import json
import uuid

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.domain.errors import QuotaExceeded
from app.domain.models import GeneratedAnswer, Hit, SentenceOut
from app.graph.build import build_graph
from app.graph.nodes_analyze import Analysis
from app.graph.nodes_answer import VERDICT_PREFACE
from app.graph.nodes_verify import refusal_message
from app.graph.runner import (
    AnswerDelta,
    Final,
    Progress,
    StageDetail,
    answer_payload,
    refusal_payload,
    run_chat,
    run_config,
)
from app.graph.state import checkpoint_serde, reset_turn_update
from app.scope import COVERED_LAWS
from app.texts import DISCLAIMER, SOURCE_NOTICE
from tests.conftest import FakeLlm, FakeSearch, deps_with, sample_hits, settings_factory


def asdict_event(event) -> dict:
    return dataclasses.asdict(event)


def run(deps, question: str = "주휴수당?", *, thread_id: str | None = None):
    """그래프를 만들어 한 턴을 끝까지 돌리고 (이벤트 목록, 그래프, thread_id)를 돌려준다."""
    graph = build_graph(deps, InMemorySaver(serde=checkpoint_serde()))
    tid = thread_id or str(uuid.uuid4())
    return list(run_chat(graph, tid, question)), graph, tid


def stages(events) -> list[str]:
    return [e.stage for e in events if isinstance(e, Progress)]


def details(events) -> list[tuple[str, str]]:
    return [(e.stage, e.text) for e in events if isinstance(e, StageDetail)]


def schemas(llm) -> list[str]:
    return [p.schema.__name__ for p in llm.prompts]


# --- 정상 경로 ---------------------------------------------------------------------------------------


# 일상 상담 한 턴: 진행 단계가 순서대로 나오고, 마지막에 출처가 붙은 답변(Final)이 나온다.
def test_happy_path_streams_progress_then_final_answer():
    deps = deps_with(FakeLlm.script())
    events, _, _ = run(deps)
    assert stages(events) == ["analyze", "search", "rerank", "sufficiency", "generate", "verify"]
    assert all(e.label for e in events if isinstance(e, Progress))
    final = events[-1]
    assert isinstance(final, Final) and final.payload["refused"] is False and final.payload["preface"] is None
    assert final.payload["sentences"][0]["citation_ids"] == ["doc1"]
    assert final.payload["sources"][0]["id"] == "doc1" and final.payload["sources"][0]["label"] == "근로기준법 제51조"
    assert final.payload["notice"] == [DISCLAIMER, SOURCE_NOTICE]
    assert deps.llm.call_count == 3  # 질문 분석, 생성, 검증


# 단계가 끝난 뒤의 요약(StageDetail)이 해당 단계 시작(Progress) 바로 뒤에 나온다: 검색 → 찾은 조문, 근거 확인 → 충분함.
def test_stage_details_follow_their_stage_and_summarise_it():
    events, _, _ = run(deps_with(FakeLlm.script()))
    kinds = [
        ("진행", e.stage)
        if isinstance(e, Progress)
        else ("요약", e.stage)
        if isinstance(e, StageDetail)
        else ("텍스트",)
        if isinstance(e, AnswerDelta)
        else ("끝",)
        for e in events
    ]
    assert kinds == [
        ("진행", "analyze"),
        ("진행", "search"), ("요약", "search"),
        ("진행", "rerank"),
        ("진행", "sufficiency"), ("요약", "sufficiency"),
        ("진행", "generate"),
        ("진행", "verify"),
        ("텍스트",),
        ("끝",),
    ]  # fmt: skip
    # 찾은 조문은 앞의 3개만 이름으로 보이고, 더 있으면 "…"를 붙인다(같은 법령명은 한 번만 쓴다).
    assert details(events) == [("search", "근로기준법 제51조, 제52조, 제53조 …"), ("sufficiency", "충분함")]


# 검색 결과가 없으면 요약도 그렇게 말한다.
def test_search_detail_when_nothing_was_found():
    events, _, _ = run(deps_with(FakeLlm.script(), search=FakeSearch([])))
    assert details(events)[0] == ("search", "찾은 조문 없음")


# 근거가 부족하다고 판단하면 요약이 "부족함"이고, 다시 찾는 단계의 라벨에 재시도 횟수(1/2)가 붙는다.
def test_insufficient_evidence_is_summarised_and_retry_counter_is_shown():
    deps = deps_with(FakeLlm.script(), settings=settings_factory(rerank_min_score=0.95))  # 점수 0.9 < 0.95 → 부족
    events, _, _ = run(deps)
    assert ("sufficiency", "부족함") in details(events)
    searches = [e.label for e in events if isinstance(e, Progress) and e.stage == "search"]
    assert "다시 찾는 중 (1/2)" in searches[1] and "(" not in searches[0]


# 다시 쓰는 단계의 라벨에도 횟수가 붙는다.
def test_regeneration_label_shows_the_counter():
    events, _, _ = run(deps_with(FakeLlm.script(first_answer_citations=["doc9"])))
    generates = [e.label for e in events if isinstance(e, Progress) and e.stage == "generate"]
    assert generates[0] == "답변을 작성하는 중" and generates[1] == "답변을 다시 쓰는 중 (1/2)"


# 화면에 보이는 텍스트는 스트리밍된다: 검증을 통과한 답변이 문장 단위(AnswerDelta)로 순서대로 먼저 나가고, 그다음 Final이 나간다.
def test_verified_answer_text_is_streamed_before_final():
    events, _, _ = run(deps_with(FakeLlm.script()))
    deltas = [e for e in events if isinstance(e, AnswerDelta)]
    assert deltas == [AnswerDelta("sentence", 0, "주휴수당은 유급휴일에 대한 임금입니다.")]
    assert events.index(deltas[0]) == len(events) - 2  # Final 바로 앞이고, 검증 단계가 끝난 뒤다


# 스트리밍된 조각을 이어 붙이면 Final의 텍스트와 정확히 같다(화면이 두 경로로 서로 다른 글을 보게 되면 안 된다).
def test_streamed_text_equals_final_text_including_preface():
    events, _, _ = run(deps_with(FakeLlm.script(qclass="verdict_request")), "제가 부당해고 맞죠?")
    deltas = [e for e in events if isinstance(e, AnswerDelta)]
    payload = events[-1].payload
    assert (deltas[0].part, deltas[0].text) == ("preface", VERDICT_PREFACE)
    assert [d.text for d in deltas] == [payload["preface"], *(s["text"] for s in payload["sentences"])]


# 거절 문구도 같은 방식으로 스트리밍된다.
def test_refusal_message_is_streamed_too():
    events, _, _ = run(deps_with(FakeLlm.script(qclass="out_of_scope")), "전세 보증금 돌려받는 법")
    deltas = [e for e in events if isinstance(e, AnswerDelta)]
    assert deltas == [AnswerDelta("preface", 0, events[-1].payload["preface"])]


# 검증을 통과하기 전의 초안은 어떤 이벤트에도 실려 나가지 않는다(스트리밍으로 답변이 새면 안 된다).
def test_draft_text_never_reaches_client_events():
    llm = FakeLlm.script(first_answer_with_text="DRAFT-SENTINEL-123", verification_fails_once=True)
    events, _, _ = run(deps_with(llm))
    # 초안이 정말 만들어졌고(생성 호출이 두 번 = 첫 초안은 버려지고 다시 씀) 그런데도 어떤 이벤트에도 없다는 뜻이다.
    assert schemas(llm).count("GeneratedAnswer") == 2
    assert all(
        "DRAFT-SENTINEL-123" not in json.dumps(asdict_event(e), ensure_ascii=False)
        for e in events
        if isinstance(e, (Progress, StageDetail, AnswerDelta))
    )
    assert isinstance(events[-1], Final) and "DRAFT-SENTINEL-123" not in json.dumps(events[-1].payload, ensure_ascii=False)


# 스트리밍 없이 invoke로 불러도(평가 러너가 쓰는 방식) 진행 이벤트 기록 때문에 죽지 않고, 호출 수가 쌓인다.
def test_graph_invoke_works_without_streaming():
    deps = deps_with(FakeLlm.script())
    graph = build_graph(deps, InMemorySaver(serde=checkpoint_serde()))
    values = graph.invoke(reset_turn_update("주휴수당?"), run_config(str(uuid.uuid4())))
    assert values["llm_calls"] == 3 and values["answer"] is not None


# --- 거절 경로 ---------------------------------------------------------------------------------------


# 범위 밖 질문은 LLM 1번(질문 분석)만 쓰고 검색하지 않으며, 거절 문구가 나간다.
def test_out_of_scope_uses_one_llm_call_and_no_search():
    deps = deps_with(FakeLlm.script(qclass="out_of_scope"))
    events, _, _ = run(deps, "전세 보증금 돌려받는 법")
    assert deps.search.calls == 0 and deps.llm.call_count == 1 and stages(events) == ["analyze"]
    final = events[-1]
    assert final.payload["refused"] is True and final.payload["preface"] == refusal_message("out_of_scope", COVERED_LAWS)
    assert final.payload["sentences"] == [] and final.payload["sources"] == []


# 근로 분야지만 수집 범위 밖(예: 실업급여)은 "근거 없음"으로 거절하고 수집 범위를 알려 준다. 검색은 하지 않는다.
def test_uncovered_labor_refuses_with_covered_law_list_and_no_search():
    deps = deps_with(FakeLlm.script(qclass="uncovered_labor"))
    events, _, _ = run(deps, "실업급여 얼마나 받아?")
    assert deps.search.calls == 0 and deps.llm.call_count == 1
    assert events[-1].payload["preface"] == refusal_message("no_evidence", COVERED_LAWS)
    assert "근로기준법" in events[-1].payload["preface"]


# 검색 결과가 하나도 없으면 재검색을 시도하다가(새 검색어는 만들 수 있다) 끝내 "근거 없음"으로 거절한다.
def test_no_search_results_ends_in_no_evidence_refusal():
    deps = deps_with(FakeLlm.script(), search=FakeSearch([]))
    events, _, _ = run(deps)
    assert events[-1].payload["refused"] is True and "현재 수집한 법령 범위에 없습니다" in events[-1].payload["preface"]
    assert deps.llm.call_count <= 8


# 검증이 계속 실패해도 질문 하나의 LLM 호출은 상한(8회)을 넘지 않고, 호출 한도 문구로 끝난다.
def test_total_llm_calls_never_exceed_budget():
    deps = deps_with(FakeLlm.script(always_unsupported=True))
    events, _, _ = run(deps, "q")
    assert deps.llm.call_count <= 8
    assert events[-1].payload["refused"] is True and events[-1].payload["preface"] == refusal_message("limit", COVERED_LAWS)


# --- 다시 쓰기·다시 찾기 ------------------------------------------------------------------------------


# 없는 문서를 인용하면 코드 검사가 잡아 다시 쓰고(검증 LLM은 아끼고), 두 번째 답이 나간다. 위반 내용이 재생성 프롬프트에 들어간다.
def test_bad_citation_is_regenerated_without_spending_a_verification_call():
    llm = FakeLlm.script(first_answer_citations=["doc9"])
    events, graph, tid = run(deps_with(llm))
    assert schemas(llm) == ["Analysis", "GeneratedAnswer", "GeneratedAnswer", "Verification"]
    assert "존재하지 않는 인용 ID: doc9" in llm.prompts[2].user
    assert events[-1].payload["refused"] is False and graph.get_state(run_config(tid)).values["regen_count"] == 1


# 사건 판단 문장이 있다는 검증 결과도 다시 쓰게 한다.
def test_verdict_sentence_triggers_regeneration():
    llm = FakeLlm.script(verdict_found_once=True)
    events, _, _ = run(deps_with(llm))
    assert schemas(llm) == ["Analysis", "GeneratedAnswer", "Verification", "GeneratedAnswer", "Verification"]
    assert events[-1].payload["refused"] is False


# 근거 문장이 전부 불일치하면 검색어를 바꿔 다시 찾고(재검색), 새 근거로 쓴 답이 나간다.
def test_unsupported_answer_triggers_research_with_a_new_query():
    llm = FakeLlm.script(verification_fails_once=True)
    deps = deps_with(llm)
    events, graph, tid = run(deps)
    assert schemas(llm) == ["Analysis", "GeneratedAnswer", "Verification", "Refined", "GeneratedAnswer", "Verification"]
    assert deps.search.calls == 2 and events[-1].payload["refused"] is False
    assert graph.get_state(run_config(tid)).values["research_count"] == 1
    assert stages(events).count("search") == 2  # 재검색도 "검색" 단계로 보이고,
    labels = [e.label for e in events if isinstance(e, Progress) and e.stage == "search"]
    assert "다시" in labels[1] and "다시" not in labels[0]  # 두 번째는 "검색어를 바꿔 다시 찾는 중 (1/2)"


# --- 사건 판단 요청 ----------------------------------------------------------------------------------


# 사건 판단 요청이면 정해진 서두 문장이 preface로 나가고, 답변은 근거만 보여 준다.
def test_verdict_request_gets_the_fixed_preface():
    events, _, _ = run(deps_with(FakeLlm.script(qclass="verdict_request")), "제가 부당해고 맞죠?")
    assert events[-1].payload["preface"] == VERDICT_PREFACE and events[-1].payload["refused"] is False


# --- 대화 이어가기 -----------------------------------------------------------------------------------


# 같은 대화로 두 번째 턴: 호출 수·재시도 카운터는 새로 시작하고, 이력은 쌓여 두 번째 질문 분석에 전달된다.
def test_second_turn_starts_fresh_but_sees_history():
    llm = FakeLlm.script()
    deps = deps_with(llm)
    graph, tid = build_graph(deps, InMemorySaver(serde=checkpoint_serde())), str(uuid.uuid4())
    list(run_chat(graph, tid, "주휴수당이 뭐야?"))
    list(run_chat(graph, tid, "그럼 알바는요?"))
    values = graph.get_state(run_config(tid)).values
    assert values["llm_calls"] == 3  # 두 번째 턴도 0에서 시작해 3회(둘째 턴만 센 값)
    assert [m.type for m in values["messages"]] == ["human", "ai", "human", "ai"]
    analysis_prompts = [p for p in llm.prompts if p.schema is Analysis]
    first, second = analysis_prompts[0].user, analysis_prompts[1].user
    assert "<앞선 대화>" not in first  # 첫 턴에는 이력이 없다
    assert "<앞선 대화>" in second and "주휴수당이 뭐야?" in second.split("<질문>")[0]  # 둘째 턴은 첫 질문을 이력으로 본다


# --- 오류·계약 ---------------------------------------------------------------------------------------


# 외부 서비스의 한도 초과는 삼키지 않고 위로 올린다(사용자 안내와 횟수 차감 여부는 API 계층이 정한다).
def test_quota_error_propagates_to_the_caller():
    deps = deps_with(FakeLlm([QuotaExceeded("한도")]))
    with pytest.raises(QuotaExceeded):
        run(deps)


# 그래프가 최종 답변(Final) 없이 끝나면 조용히 넘어가지 않고 오류를 낸다.
def test_run_chat_without_final_event_is_an_error():
    class Silent:
        def stream(self, *_a, **_k):
            yield Progress("analyze", "x")

    with pytest.raises(RuntimeError):
        list(run_chat(Silent(), "t", "q"))


def test_run_config_has_safety_recursion_limit():
    cfg = run_config("t")
    assert cfg["recursion_limit"] == 25 and cfg["configurable"]["thread_id"] == "t"


# --- 화면용 최종 값(payload) -------------------------------------------------------------------------


def payload_state(*sentences: SentenceOut, qclass: str = "counsel") -> dict:
    return {"reranked": sample_hits(3), "answer": GeneratedAnswer(sentences=list(sentences)), "qclass": qclass}


# 출처는 실제로 인용된 문서만, 처음 인용된 순서로, 중복 없이 나간다. 출처 항목에는 화면이 쓸 라벨·주소·조문 정보가 있다.
def test_sources_are_only_cited_documents_in_first_citation_order():
    p = answer_payload(
        payload_state(
            SentenceOut(text="A", citation_ids=["doc2", "doc1"], kind="evidence"),
            SentenceOut(text="B", citation_ids=["doc2"], kind="evidence"),
        )
    )
    assert [s["id"] for s in p["sources"]] == ["doc2", "doc1"]
    first = p["sources"][0]
    assert first["label"] == "근로기준법 제52조" and first["url"].endswith("/제52조") and first["law_name"] == "근로기준법"
    assert first["article_no"] == "52" and first["branch_no"] is None and "유급휴일" in first["text"]
    assert p["notice"] == [DISCLAIMER, SOURCE_NOTICE] and p["refused"] is False


# 가지 조문("43의2")은 조문번호와 가지번호로 나눠서 내보낸다(화면의 formatSource 계약).
def test_branch_article_is_split_for_the_screen():
    state = payload_state(SentenceOut(text="A", citation_ids=["doc1"], kind="evidence"))
    chunk = state["reranked"][0].chunk
    state["reranked"][0] = Hit(dataclasses.replace(chunk, article_no="43의2"), 0.9, 1)
    source = answer_payload(state)["sources"][0]
    assert (source["article_no"], source["branch_no"], source["label"]) == ("43", "2", "근로기준법 제43조의2")


# 사건 판단 요청이면 서두를 preface로 올리고, 답변 첫 문장이 같은 문장이면 중복해서 보이지 않게 뺀다.
def test_verdict_preface_is_lifted_and_not_duplicated():
    p = answer_payload(
        payload_state(
            SentenceOut(text=VERDICT_PREFACE, citation_ids=[], kind="general"),
            SentenceOut(text="A", citation_ids=["doc1"], kind="evidence"),
            qclass="verdict_request",
        )
    )
    assert p["preface"] == VERDICT_PREFACE and [s["text"] for s in p["sentences"]] == ["A"]


# 일반 상담에서는 서두가 없고 모든 문장이 그대로 나간다.
def test_counsel_answer_keeps_all_sentences_and_has_no_preface():
    p = answer_payload(
        payload_state(
            SentenceOut(text=VERDICT_PREFACE, citation_ids=[], kind="general"),
            SentenceOut(text="A", citation_ids=["doc1"], kind="evidence"),
        )
    )
    assert p["preface"] is None and len(p["sentences"]) == 2


def test_refusal_payload_shape():
    p = refusal_payload("거절 문구")
    assert p == {"refused": True, "preface": "거절 문구", "sentences": [], "sources": [], "notice": [DISCLAIMER, SOURCE_NOTICE]}
