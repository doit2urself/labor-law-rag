"""그래프에서 수동 기록 이벤트(EventSink)가 남는 곳과 그 내용을 시험한다 (Task 16).

Langfuse SDK 자체(마스킹 훅, trace id, 점수)는 test_langfuse_setup.py가 맡는다. 여기서는 그래프가 **무엇을 기록하는가**와
**마스킹이 기록에만 걸리고 LLM 입력에는 걸리지 않는가**를 본다.
"""

import json
import uuid

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langgraph.checkpoint.memory import InMemorySaver

from app.domain.errors import ExternalServiceError
from app.graph.build import build_graph
from app.graph.nodes_analyze import Analysis
from app.graph.runner import Final, run_chat
from app.graph.state import checkpoint_serde
from tests.conftest import FakeLlm, FakeRerankStage, RecordingSink, ScriptedLlm, deps_with


def _run(llm, sink, question="주 15시간 일하면 주휴수당 받나요?", **deps_overrides):
    """가짜 부품 그래프를 돌려 이벤트 목록을 돌려준다."""
    graph = build_graph(deps_with(llm, sink=sink, **deps_overrides), InMemorySaver(serde=checkpoint_serde()))
    return list(run_chat(graph, str(uuid.uuid4()), question))


# 기록에는 전화번호가 가려져서 남고, LLM에는 원문이 그대로 간다.
def test_masking_applies_to_records_not_to_llm_input():
    rec = RecordingSink()
    llm = FakeLlm.script(qclass="counsel")
    list(
        run_chat(
            build_graph(deps_with(llm, sink=rec), InMemorySaver(serde=checkpoint_serde())),
            str(uuid.uuid4()),
            "제 번호는 010-1234-5678 입니다",
        )
    )
    assert "010-1234-5678" in llm.prompts[0].user
    assert "010-1234-5678" not in json.dumps(rec.events, ensure_ascii=False)


# 질문 분석이 만든 재작성 질의에 번호가 섞여 있어도(모델이 그대로 옮겼다면) 기록에는 가려서 남는다.
def test_rewritten_query_with_pii_is_masked_in_the_record():
    class PiiRewriteLlm(ScriptedLlm):
        def complete_json(self, system, user, schema, *, tags=None):
            result = super().complete_json(system, user, schema, tags=tags)
            if isinstance(result, Analysis):
                return result.model_copy(update={"rewritten_query": "연락처 010-9999-8888 주휴수당"})
            return result

    rec = RecordingSink()
    _run(PiiRewriteLlm(), rec)
    assert rec.payloads("analyze")[0]["rewritten_query"] == "연락처 [전화번호] 주휴수당"
    assert "010-9999-8888" not in json.dumps(rec.events, ensure_ascii=False)


# 정상 경로(상담 질문 → 통과)에서 단계별 기록이 순서대로 남고, 갈림길 결과와 최종 결과도 남는다.
def test_normal_path_records_each_stage_in_order():
    rec = RecordingSink()
    events = _run(FakeLlm.script(qclass="counsel"), rec)
    assert isinstance(events[-1], Final)
    assert rec.names() == ["analyze", "search", "rerank", "sufficiency", "verify", "branch", "final"]
    assert rec.payloads("analyze")[0]["qclass"] == "counsel"
    assert rec.payloads("search")[0]["candidates"] > 0
    assert rec.payloads("rerank")[0] == {"kept": 5, "used_reranker": True, "fallback": False}
    assert rec.payloads("sufficiency")[0]["sufficient"] is True
    assert rec.payloads("branch")[0]["outcome"] == "pass"
    final = rec.payloads("final")[0]
    assert final["refused"] is False and final["sources"] >= 1 and final["llm_calls"] >= 3


# 증거 문장이 전부 근거와 안 맞으면 verify_node가 "근거 부족"으로 보고 다시 찾는다: 갈림길 기록이 research → pass 순서로 남는다.
def test_research_is_recorded_as_branch_outcome():
    rec = RecordingSink()
    _run(FakeLlm.script(verification_fails_once=True), rec)
    assert [p["outcome"] for p in rec.payloads("branch")] == ["research", "pass"]
    final = rec.payloads("final")[0]
    assert (final["research_count"], final["regen_count"]) == (1, 0)
    assert rec.payloads("search")[-1]["attempt"] == 1  # 두 번째 검색은 재검색 1회째로 기록된다


# 사건 판단 문장이 나오면 같은 문서로 다시 쓴다: 갈림길 기록이 regenerate → pass 순서로 남고 다시 쓴 횟수가 최종 기록에 남는다.
def test_regeneration_is_recorded_as_branch_outcome():
    rec = RecordingSink()
    _run(FakeLlm.script(verdict_found_once=True), rec)
    assert [p["outcome"] for p in rec.payloads("branch")] == ["regenerate", "pass"]
    assert rec.payloads("final")[0]["regen_count"] == 1


# 코드 검사 위반(없는 인용 ID)은 갈림길 기록에 위반 코드로 남는다(실패 원인).
def test_code_check_violation_is_recorded_with_its_code():
    rec = RecordingSink()
    _run(FakeLlm.script(first_answer_citations=["doc999"]), rec)
    first_branch = rec.payloads("branch")[0]
    assert first_branch["outcome"] == "regenerate"
    assert first_branch["violations"], "위반 코드가 비어 있다"
    assert all(isinstance(code, str) for code in first_branch["violations"])


# 근로 분야가 아닌 질문은 검색 없이 거절된다: 분석 기록 뒤 바로 최종 기록(거절 종류 포함).
def test_out_of_scope_refusal_is_recorded():
    rec = RecordingSink()
    _run(FakeLlm.script(qclass="out_of_scope"), rec, question="전세 보증금 돌려받는 법")
    assert rec.names() == ["analyze", "final"]
    assert rec.payloads("final")[0] == {
        "refused": True,
        "kind": "out_of_scope",
        "sources": 0,
        "llm_calls": 1,
        "regen_count": 0,
        "research_count": 0,
    }


# 리랭커를 못 써서 RRF 순위로 대신했으면(폴백) 리랭크 기록에 그 사실이 남는다.
def test_reranker_fallback_is_recorded():
    rec = RecordingSink()
    _run(FakeLlm.script(), rec, rerank=FakeRerankStage(used_reranker=False))
    assert rec.payloads("rerank")[0]["fallback"] is True
    assert rec.payloads("rerank")[0]["used_reranker"] is False


# 기록 저장소가 고장 나도 답변은 나간다(관측이 서비스를 멈추게 하면 안 된다). 질문 원문은 경고 로그에도 남지 않는다.
def test_a_failing_sink_does_not_break_the_answer(caplog):
    def broken(name, payload):
        raise RuntimeError("boom 010-1234-5678")

    with caplog.at_level("WARNING"):
        events = _run(FakeLlm.script(), broken, question="제 번호는 010-1234-5678 입니다")
    assert isinstance(events[-1], Final) and events[-1].payload["refused"] is False
    assert caplog.records, "실패를 경고로 남겨야 한다"
    assert "010-1234-5678" not in caplog.text


# sink가 없으면(기본값) 아무것도 기록하지 않고 그대로 동작한다.
def test_no_sink_means_no_recording():
    events = _run(FakeLlm.script(), None)
    assert isinstance(events[-1], Final)


# 그래프가 외부 서비스 오류로 중간에 죽어도, 거기까지의 기록은 남고 오류는 호출한 쪽으로 올라간다.
def test_records_before_a_failure_are_kept_and_error_propagates():
    rec = RecordingSink()
    llm = FakeLlm([ExternalServiceError("analyze failed")])
    with pytest.raises(ExternalServiceError):
        _run(llm, rec)
    assert rec.names() == []


# run_chat에 넘긴 추가 설정(콜백)이 그래프 실행에 전달된다. 대화 번호(thread_id)는 추가 설정으로 바꿀 수 없다.
def test_run_chat_passes_extra_config_but_keeps_thread_id():
    class Counter(BaseCallbackHandler):
        def __init__(self):
            self.chain_names: list[str] = []

        def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs):
            self.chain_names.append(kwargs.get("name") or "")

    counter = Counter()
    graph = build_graph(deps_with(FakeLlm.script()), InMemorySaver(serde=checkpoint_serde()))
    thread = str(uuid.uuid4())
    extra = {"callbacks": [counter], "run_name": "labor-rag-chat", "configurable": {"thread_id": "attacker"}}
    list(run_chat(graph, thread, "주휴수당 알려줘", config_extra=extra))
    assert "labor-rag-chat" in counter.chain_names
    assert graph.get_state({"configurable": {"thread_id": thread}}).values["original_question"] == "주휴수당 알려줘"
    assert graph.get_state({"configurable": {"thread_id": "attacker"}}).values == {}
