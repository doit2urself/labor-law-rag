"""체크포인트 직렬화 허용 목록 테스트: 대화 상태를 저장했다가 읽을 때 우리 도메인 객체가 망가지지 않는지 시험한다.

배경: LangGraph는 상태를 체크포인트(운영에서는 SQLite)에 저장했다가 같은 대화의 다음 턴에 읽어 온다. 상태에는 우리가 만든
객체(Hit, Chunk, GeneratedAnswer …)가 들어 있다. 기본 설정은 "등록 안 된 타입도 읽되 경고"이고, 앞으로는 차단된다고 한다.
허용 목록(allowed_msgpack_modules)을 명시하면 그 밖의 타입은 차단되고, 차단된 타입은 **조용히 dict로 복원된다** —
그러면 다음 턴에서 `hit.chunk.text`가 AttributeError로 터진다. 그래서 목록이 상태가 쓰는 타입을 빠짐없이 담았는지 자동으로 검사한다.
"""

from __future__ import annotations

import dataclasses
import logging
import typing
import uuid

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel

from app.domain import models
from app.domain.models import Chunk, GeneratedAnswer, Hit, Verification
from app.graph.build import build_graph
from app.graph.runner import run_chat, run_config
from app.graph.state import STATE_TYPES, GraphState, checkpoint_serde
from tests.conftest import FakeLlm, deps_with


def domain_types_used_by(tp, found: set[type]) -> None:
    """타입 힌트 `tp` 안에 (겹겹이 들어 있는 것까지) 쓰인 app.domain.models의 클래스를 `found`에 모은다."""
    for arg in typing.get_args(tp):
        domain_types_used_by(arg, found)
    if isinstance(tp, type) and tp.__module__ == models.__name__ and tp not in found:
        found.add(tp)
        if dataclasses.is_dataclass(tp):
            for hint in typing.get_type_hints(tp).values():
                domain_types_used_by(hint, found)
        elif issubclass(tp, BaseModel):
            for field in tp.model_fields.values():
                domain_types_used_by(field.annotation, found)


# 상태(GraphState)의 어느 필드든 우리 도메인 타입을 쓰면 허용 목록에 있어야 한다. 새 필드를 더하면서 목록을 깜빡하면 여기서 잡힌다.
def test_allowlist_covers_every_domain_type_used_by_the_state():
    found: set[type] = set()
    for hint in typing.get_type_hints(GraphState).values():
        domain_types_used_by(hint, found)
    assert {Hit, Chunk, GeneratedAnswer, Verification} <= found  # 탐색이 실제로 동작한다는 확인
    assert found <= set(STATE_TYPES), f"허용 목록에 없는 타입: {sorted(t.__name__ for t in found - set(STATE_TYPES))}"


def run_two_turns(serde) -> tuple[typing.Any, dict]:
    graph = build_graph(deps_with(FakeLlm.script()), InMemorySaver(serde=serde))
    tid = str(uuid.uuid4())
    list(run_chat(graph, tid, "주휴수당이 뭐야?"))
    list(run_chat(graph, tid, "그럼 알바는요?"))
    return graph, run_config(tid)


# 저장했다 읽어도 진짜 객체로 돌아온다(dict가 아니다). 같은 대화의 둘째 턴이 이 상태 위에서 정상 동작했다는 뜻이기도 하다.
def test_state_round_trips_as_real_objects_without_warnings(caplog):
    with caplog.at_level(logging.WARNING):
        graph, cfg = run_two_turns(checkpoint_serde())
    values = graph.get_state(cfg).values
    assert isinstance(values["reranked"][0], Hit) and isinstance(values["reranked"][0].chunk, Chunk)
    assert isinstance(values["answer"], GeneratedAnswer) and isinstance(values["verification"], Verification)
    assert not [r for r in caplog.records if "unregistered" in r.getMessage() or "Blocked" in r.getMessage()]


# 대조: 목록에서 Chunk를 빼면 Hit 안의 chunk가 dict로 망가진다. 위의 "빠짐없는 목록" 검사가 왜 필요한지 보여 주는 시험이다.
def test_a_missing_type_silently_degrades_to_a_dict():
    narrow = JsonPlusSerializer(allowed_msgpack_modules=[(Hit.__module__, "Hit")])  # Chunk 누락
    graph = build_graph(deps_with(FakeLlm.script()), InMemorySaver(serde=narrow))
    tid = str(uuid.uuid4())
    list(run_chat(graph, tid, "주휴수당이 뭐야?"))
    assert isinstance(graph.get_state(run_config(tid)).values["reranked"][0].chunk, dict)
