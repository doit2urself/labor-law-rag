"""Langfuse SDK 연결(app/observability/langfuse_setup.py)을 시험한다 (Task 16).

네트워크를 쓰지 않는다: 가짜 키와 **메모리 exporter**를 끼운 진짜 Langfuse SDK를 돌려서, Langfuse Cloud로 나갈 span이
어떤 모양인지(개인정보가 가려졌는지, trace id·세션·사용자·토큰 사용량이 붙었는지)를 그대로 들여다본다.
점수(피드백)는 SDK의 `create_score` 호출 인자만 확인한다(실제 전송은 Task 16 Step 5의 실측에서 본다).
"""

import json
import uuid
from types import MappingProxyType

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langfuse.types import MaskOtelSpansParams, OtelSpanData, OtelSpanIdentifier
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel
from typing_extensions import TypedDict

from app.adapters.gemini import GeminiLlm
from app.domain.errors import ExternalServiceError
from app.graph.build import build_graph
from app.graph.runner import Final, run_chat
from app.graph.state import checkpoint_serde
from app.observability.langfuse_setup import LangfuseTracing, NullTracing, build_langfuse, mask_otel_spans
from tests.conftest import FakeLlm, deps_with, settings_factory

PHONE = "010-1234-5678"
# 개인정보가 새는지 볼 때는 번호의 뒷부분만 찾는다(앞부분 "010"은 다른 숫자와 겹칠 수 있다).
PHONE_TAIL = "1234-5678"


@pytest.fixture
def traced():
    """(tracing, exporter): 메모리 exporter를 끼운 진짜 Langfuse 연결. 시험마다 새 공개 키를 써서 서로 섞이지 않는다.

    SDK는 공개 키마다 연결을 하나만 만들어 재사용하므로, 키가 같으면 두 번째 시험의 exporter가 무시된다. 그래서 키를 매번 바꾼다.
    """
    exporter = InMemorySpanExporter()
    settings = settings_factory(langfuse_public_key=f"pk-lf-test-{uuid.uuid4().hex}")
    tracing = build_langfuse(settings, span_exporter=exporter)
    yield tracing, exporter
    tracing.shutdown()


def _chat(tracing, llm, question, *, username="test"):
    """진짜 그래프(가짜 부품)를 Langfuse 콜백·기록과 함께 한 턴 돌리고, 내보낸 span이 모이도록 flush한다."""
    message_id, thread_id = str(uuid.uuid4()), str(uuid.uuid4())
    graph = build_graph(deps_with(llm, sink=tracing.record_event), InMemorySaver(serde=checkpoint_serde()))
    config = tracing.run_config(message_id=message_id, thread_id=thread_id, username=username)
    events = list(run_chat(graph, thread_id, question, config_extra=config))
    tracing.flush()
    return events, message_id, thread_id


def _everything_exported(spans) -> str:
    """내보낸 span의 모든 글(속성 값, 상태 설명, 이벤트 속성)을 한 덩어리로 합친다. 개인정보가 어디에도 없는지 볼 때 쓴다."""
    parts: list[str] = []
    for span in spans:
        parts += [str(v) for v in span.attributes.values()]
        parts.append(span.status.description or "")
        for event in span.events:
            parts += [str(v) for v in event.attributes.values()]
    return "\n".join(parts)


# ---- mask_otel_spans (훅 단독) ----


def _otel_span(attributes: dict, span_id: str = "a" * 16):
    identifier = OtelSpanIdentifier(trace_id="1" * 32, span_id=span_id)
    data = OtelSpanData(
        trace_id=identifier.trace_id,
        span_id=identifier.span_id,
        parent_span_id=None,
        name="n",
        instrumentation_scope_name="langfuse",
        instrumentation_scope_version=None,
        attributes=MappingProxyType(attributes),
        resource_attributes=MappingProxyType({}),
    )
    return identifier, data


def _apply_hook(*spans):
    params = MaskOtelSpansParams(spans=MappingProxyType(dict(spans)))
    return mask_otel_spans(params=params)


# 입력 속성(JSON 글, 한글이 \uXXXX로 이스케이프됨)의 번호를 가리고, 바뀌지 않은 속성은 패치에 넣지 않는다.
def test_hook_masks_escaped_json_attribute_and_only_that_attribute():
    raw = '{"q": "\\uc81c \\ubc88\\ud638\\ub294010-1234-5678"}'
    identifier, data = _otel_span({"langfuse.observation.input": raw, "langfuse.observation.type": "chain", "n": 3})
    result = _apply_hook((identifier, data))
    patch = result.span_patches[identifier]
    assert set(patch.set_attributes) == {"langfuse.observation.input"}
    assert json.loads(patch.set_attributes["langfuse.observation.input"]) == {"q": "제 번호는[전화번호]"}


# 문자열 목록 속성(예: 태그)도 항목마다 가린다.
def test_hook_masks_string_sequence_attributes():
    identifier, data = _otel_span({"langfuse.trace.tags": ("a@b.com", "graph:step:1")})
    patch = _apply_hook((identifier, data)).span_patches[identifier]
    assert list(patch.set_attributes["langfuse.trace.tags"]) == ["[이메일]", "graph:step:1"]


# 어떤 속성을 가리다가 예외가 나면 그 속성만 지워서 보내고(실패에 닫힘), 다른 속성은 정상으로 가린다. 배치 전체를 버리지 않는다.
def test_hook_deletes_an_attribute_it_cannot_mask_and_keeps_masking_the_rest(monkeypatch):
    import app.observability.langfuse_setup as setup

    real = setup.mask_serialized

    def flaky(text):
        if "BOOM" in text:
            raise RuntimeError("cannot mask")
        return real(text)

    monkeypatch.setattr(setup, "mask_serialized", flaky)
    identifier, data = _otel_span({"bad": "BOOM", "good": f"전화 {PHONE}"})
    patch = _apply_hook((identifier, data)).span_patches[identifier]
    assert tuple(patch.delete_attributes) == ("bad",)
    assert patch.set_attributes == {"good": "전화 [전화번호]"}


# 가릴 것이 하나도 없으면 None을 돌려준다(SDK가 배치를 그대로 보낸다).
def test_hook_returns_none_when_nothing_to_mask():
    _, data = _otel_span({"langfuse.observation.type": "chain", "langfuse.observation.input": '{"q": "주휴수당"}'})
    identifier = OtelSpanIdentifier(data.trace_id, data.span_id)
    assert _apply_hook((identifier, data)) is None


# 여러 span 중 개인정보가 있는 span에만 패치가 붙는다.
def test_hook_patches_only_spans_that_need_it():
    clean_id, clean = _otel_span({"k": "평범한 글"}, span_id="b" * 16)
    dirty_id, dirty = _otel_span({"k": f"전화 {PHONE}"}, span_id="c" * 16)
    result = _apply_hook((clean_id, clean), (dirty_id, dirty))
    assert set(result.span_patches) == {dirty_id}


# ---- 실제 SDK를 거친 끝에서 끝까지 ----


# 번호는 LLM에는 원문 그대로 가고, Langfuse로 나가는 모든 span(입력·출력·메타데이터·이벤트 기록)에서는 가려진다.
def test_pii_never_leaves_in_exported_spans_but_reaches_the_llm(traced):
    tracing, exporter = traced
    llm = FakeLlm.script()
    events, _, _ = _chat(tracing, llm, f"제 번호는{PHONE}입니다")
    assert isinstance(events[-1], Final)
    assert PHONE in llm.prompts[0].user  # LLM 입력은 원문
    spans = exporter.get_finished_spans()
    assert len(spans) > 5
    assert PHONE_TAIL not in _everything_exported(spans)
    assert "[전화번호]" in _everything_exported(spans)  # 가렸다는 표시가 실제로 남았다(아예 안 보낸 것이 아니다)


# 수동 기록 이벤트(analyze, search, …, final)가 같은 trace 안에 event로 남는다.
def test_manual_events_are_exported_as_events_in_the_same_trace(traced):
    tracing, exporter = traced
    _, message_id, _ = _chat(tracing, FakeLlm.script(), "주휴수당 알려줘")
    spans = exporter.get_finished_spans()
    trace_ids = {format(s.get_span_context().trace_id, "032x") for s in spans}
    assert trace_ids == {tracing.trace_id_for(message_id)}
    event_names = [s.name for s in spans if s.attributes.get("langfuse.observation.type") == "event"]
    assert event_names == ["analyze", "search", "rerank", "sufficiency", "verify", "branch", "final"]


# 같은 message_id는 항상 같은 trace id가 된다(피드백이 저장소 없이도 그 trace를 찾아간다). 다른 message_id는 다른 trace다.
def test_trace_id_is_derived_from_message_id(traced):
    tracing, _ = traced
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    assert tracing.trace_id_for(a) == tracing.trace_id_for(a)
    assert tracing.trace_id_for(a) != tracing.trace_id_for(b)
    assert len(tracing.trace_id_for(a)) == 32 and int(tracing.trace_id_for(a), 16) >= 0


# 대화 번호와 사용자 이름이 Langfuse의 세션·사용자로 붙고, 실행 이름이 붙는다.
def test_session_user_and_run_name_are_attached(traced):
    tracing, exporter = traced
    _, _, thread_id = _chat(tracing, FakeLlm.script(), "주휴수당 알려줘", username="admin")
    spans = exporter.get_finished_spans()
    assert any(s.attributes.get("session.id") == thread_id for s in spans)
    assert any(s.attributes.get("user.id") == "admin" for s in spans)
    assert any(s.name == "labor-rag-chat" for s in spans)


# 노드가 예외를 내도(외부 서비스 오류 메시지에 번호가 섞여도) 오류 문구가 속성에도, span 상태 설명에도 가려진다.
# 상태 설명은 속성이 아니라서 마스킹 훅이 못 고친다 — 핸들러의 오류 문구 경로(_get_error_level_and_status_message)를 우리가 가린다.
def test_error_messages_are_masked_in_attribute_and_status(traced):
    tracing, exporter = traced
    llm = FakeLlm([ExternalServiceError(f"모델 오류: 연락처 {PHONE}")])
    with pytest.raises(ExternalServiceError):
        _chat(tracing, llm, "주휴수당 알려줘")
    tracing.flush()
    spans = exporter.get_finished_spans()
    errored = [s for s in spans if s.status.status_code.name == "ERROR"]
    assert errored, "오류 상태의 span이 있어야 이 시험이 의미가 있다"
    assert PHONE_TAIL not in _everything_exported(spans)
    assert any("[전화번호]" in (s.status.description or "") for s in errored)


# 토큰 사용량: LlmPort 어댑터가 모델 응답의 사용량을 버리지 않으면 Langfuse 생성(generation) 기록에 사용량이 붙는다.
def test_token_usage_reaches_the_generation_span(traced):
    tracing, exporter = traced

    class Out(BaseModel):
        answer: str

    usage = {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}

    class UsageChat(GenericFakeChatModel):
        """응답 메시지에 사용량이 든 가짜 채팅 모델. 구조화 출력은 원본 메시지를 함께 돌려주는 모양(include_raw)으로 흉내 낸다."""

        def with_structured_output(self, schema, *, include_raw=False, **kwargs):
            return self | RunnableLambda(lambda m: {"raw": m, "parsed": schema(answer="ok"), "parsing_error": None})

    chat = UsageChat(messages=iter([AIMessage(content="{}", usage_metadata=usage)]))
    llm = GeminiLlm("key", "fake-model", client=chat)

    class S(TypedDict, total=False):
        out: str

    def node(state: S) -> dict:
        return {"out": llm.complete_json("시스템", "사용자", Out).answer}

    builder = StateGraph(S)
    builder.add_node("ask", node)
    builder.add_edge(START, "ask")
    builder.add_edge("ask", END)
    graph = builder.compile()
    config = tracing.run_config(message_id=str(uuid.uuid4()), thread_id=str(uuid.uuid4()), username="test")
    graph.invoke({}, config)
    tracing.flush()

    generations = [s for s in exporter.get_finished_spans() if s.attributes.get("langfuse.observation.type") == "generation"]
    assert generations, "모델 호출이 generation으로 기록되지 않았다"
    recorded = json.loads(generations[0].attributes["langfuse.observation.usage_details"])
    assert recorded["input"] == 11 and recorded["output"] == 7


# ---- 피드백 점수 ----


@pytest.fixture
def scored(traced, monkeypatch):
    """create_score 호출을 가로채 인자를 적어 두는 연결. 실제 전송 없이 "무엇을 보내려 했는가"만 본다."""
    tracing, _ = traced
    calls: list[dict] = []
    monkeypatch.setattr(tracing.client, "create_score", lambda **kwargs: calls.append(kwargs))
    return tracing, calls


# 서버가 발급한 message_id의 피드백은 그 답변의 trace에 점수로 붙고, 같은 답변에 다시 보내면 같은 점수 번호로 덮어쓴다.
def test_feedback_becomes_a_score_on_the_answers_trace(scored):
    tracing, calls = scored
    message_id = str(uuid.uuid4())
    tracing.run_config(message_id=message_id, thread_id=str(uuid.uuid4()), username="test")  # 답변을 발급한 것으로 기록된다
    assert tracing.submit_feedback(message_id, 1) is True
    assert tracing.submit_feedback(message_id, -1) is True  # 마음을 바꿔 👎
    first, second = calls
    assert first["name"] == "user-feedback" and first["value"] == 1 and second["value"] == -1
    assert first["trace_id"] == tracing.trace_id_for(message_id)
    assert first["score_id"] == second["score_id"]  # 같은 점수를 덮어쓴다(점수가 계속 늘어나지 않는다)
    assert message_id in first["score_id"]  # 점수 번호는 답변마다 다르다(다른 답변의 점수를 덮어쓰지 않는다)
    assert first["data_type"] == "NUMERIC"


# 서버가 발급하지 않은 message_id는 점수로 남기지 않는다: 아무 문자열이나 보내 Langfuse 월 한도(units)를 태우지 못하게 한다.
def test_feedback_for_an_unknown_message_is_ignored(scored):
    tracing, calls = scored
    assert tracing.submit_feedback("never-issued", 1) is False
    assert calls == []


# 값은 1(👍)과 -1(👎)만 받는다.
def test_feedback_value_must_be_plus_or_minus_one(scored):
    tracing, calls = scored
    message_id = str(uuid.uuid4())
    tracing.run_config(message_id=message_id, thread_id=str(uuid.uuid4()), username="test")
    with pytest.raises(ValueError):
        tracing.submit_feedback(message_id, 5)
    assert calls == []


# 발급 기록은 크기가 한정되어 있다: 오래된 것부터 잊는다(메모리가 무한히 늘지 않는다).
def test_issued_message_ids_are_bounded(traced, monkeypatch):
    tracing, _ = traced
    small = LangfuseTracing(tracing.client, tracing.public_key, max_issued=3)
    calls: list[dict] = []
    monkeypatch.setattr(small.client, "create_score", lambda **kwargs: calls.append(kwargs))
    ids = [str(uuid.uuid4()) for _ in range(4)]
    for message_id in ids:
        small.run_config(message_id=message_id, thread_id=str(uuid.uuid4()), username="test")
    assert small.submit_feedback(ids[0], 1) is False  # 가장 오래된 것은 잊혔다
    assert small.submit_feedback(ids[3], 1) is True


# Langfuse가 고장 나서 점수를 못 보내도 예외가 서비스로 올라가지 않는다.
def test_a_failing_score_call_does_not_raise(traced, monkeypatch):
    tracing, _ = traced
    message_id = str(uuid.uuid4())
    tracing.run_config(message_id=message_id, thread_id=str(uuid.uuid4()), username="test")

    def boom(**kwargs):
        raise RuntimeError("langfuse down")

    monkeypatch.setattr(tracing.client, "create_score", boom)
    assert tracing.submit_feedback(message_id, 1) is False


# ---- 꺼진 상태 ----


# 추적을 끈 상태(NullTracing)는 아무것도 하지 않고, 그래프에 넘길 추가 설정도 비어 있다.
def test_null_tracing_does_nothing():
    null = NullTracing()
    assert null.run_config(message_id="m", thread_id="t", username="u") == {}
    null.record_event("x", {"a": 1})
    assert null.submit_feedback("m", 1) is False
    null.flush()
    null.shutdown()


# ---- 실제 API 경로(스레드풀에서 제너레이터를 한 조각씩 도는 StreamingResponse)를 거쳐도 trace가 하나로 이어진다 ----


# 채팅 API를 실제로 부르면 모든 span이 그 답변의 trace 하나에 모이고, 부모를 잃은 span이 없으며, 컨텍스트 오류 로그가 없다.
# 콜백은 요청 스레드에서 만들어지고 그래프는 스레드풀의 다른 스레드에서 한 조각씩 돌기 때문에 SDK 단독 시험(위)과는 다른 경로다.
async def test_chat_through_the_api_yields_one_connected_trace(traced, tmp_path, caplog):
    from tests.api_support import login_cookie, make_app, parse_sse, post_chat
    from tests.conftest import ADMIN_PASSWORD

    tracing, exporter = traced
    app = make_app(tmp_path, tracing=tracing, llm=FakeLlm.script())
    cookie = await login_cookie(app, "admin", ADMIN_PASSWORD)
    # sink도 이 연결로 이어 붙여야 수동 기록 이벤트까지 같은 trace에 들어온다(운영 조립은 container.py가 한다).
    graph = build_graph(deps_with(FakeLlm.script(), sink=tracing.record_event), InMemorySaver(serde=checkpoint_serde()))
    app.state.graph = graph
    with caplog.at_level("WARNING"):
        response = await post_chat(app, cookie, f"제 번호는{PHONE}입니다")
    tracing.flush()

    answer = [data for name, data in parse_sse(response.text) if name == "answer"][0]
    spans = exporter.get_finished_spans()
    expected_trace = tracing.trace_id_for(answer["message_id"])
    assert {format(s.get_span_context().trace_id, "032x") for s in spans} == {expected_trace}
    exported_ids = {s.get_span_context().span_id for s in spans}
    orphans = [s for s in spans if s.parent is not None and s.parent.span_id not in exported_ids]
    assert len(orphans) == 1 and orphans[0].name == "labor-rag-chat"  # 부모가 내보낸 목록에 없는 span은 맨 위(루트) 하나뿐이다
    assert not [r for r in caplog.records if "detach" in r.getMessage().lower() or "context" in r.getMessage().lower()]
    assert PHONE_TAIL not in _everything_exported(spans)
