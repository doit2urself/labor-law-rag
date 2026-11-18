"""LangGraph의 상태(GraphState)와 매 턴 시작 때 쓰는 초기화 값(reset_turn_update).

역할: 질문 하나가 그래프를 지나는 동안 노드들이 서로 주고받는 값을 한곳에 정의한다.
사용처: 모든 노드(nodes_*.py), 그래프 조립(build.py), 실행기(runner.py).

핵심 개념 — 상태 초기화가 필요한 이유:
같은 대화(thread_id)로 다시 그래프를 부르면 LangGraph는 체크포인트에 저장된 지난 턴의 상태를 불러와서
그 위에 새 입력을 덮는다. 새 입력에 적지 않은 필드는 지난 턴 값이 그대로 남는다. 그래서 재검색 횟수(research_count)가
지난 턴에서 2였다면 새 질문은 처음부터 "재검색 불가"로 시작하는 사고가 난다. 이를 막으려고 매 턴 첫 입력에
`reset_turn_update()`가 돌려주는 값을 넣는다.

이 파일은 `from __future__ import annotations`를 쓰지 않는다. LangGraph가 실행할 때 타입 힌트를 읽어서
필드별 합치기 규칙(reducer)을 알아내므로, 힌트가 문자열이 아니라 실제 타입이어야 안전하다.
"""

import operator
from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages
from langgraph.types import Overwrite

from app.domain.models import GeneratedAnswer, Hit, Verification, Violation


class GraphState(TypedDict, total=False):
    """그래프가 노드 사이에서 들고 다니는 값. `total=False`라서 아직 채워지지 않은 필드는 없어도 된다.

    reducer(합치기 규칙)가 붙은 필드만 "덮어쓰기"가 아니라 "더하기"로 동작한다.
    - messages: `add_messages`. 대화 이력이다. **턴마다 지우지 않는다**(이어지는 질문을 이해하는 데 필요).
      턴이 끝날 때 마지막 노드가 (사용자 질문, 최종 답변)을 덧붙이므로, 질문 분석 시점의 messages는 "이전 턴들"이다.
    - tried_queries: `operator.add`. 지금까지 시도한 검색어를 쌓는다(재검색 때 같은 검색어를 반복하지 않으려고).
    나머지는 일반 필드라서 새 값이 오면 통째로 바뀐다.
    """

    messages: Annotated[list, add_messages]
    original_question: str  # 사용자가 친 원문. 재작성 질의와 별개로 끝까지 보존한다.
    rewritten_query: str  # 질문 분석이 만든 검색용 독립 질의
    qclass: str  # QuestionClass 값: counsel / verdict_request / uncovered_labor / out_of_scope
    tried_queries: Annotated[list[str], operator.add]
    candidates: list[Hit]  # 검색 결과 상위 후보(보통 20개)
    reranked: list[Hit]  # 리랭크(또는 폴백) 뒤 상위 5개
    used_reranker: bool  # 실제로 리랭커를 썼는가(한도·오류면 False)
    sufficient: bool  # 근거가 충분하다고 판단했는가
    answer: GeneratedAnswer | None
    violations: list[Violation]  # 코드 검사가 찾은 위반
    verification: Verification | None  # LLM 근거 검증 결과
    failure_reason: str | None  # 근거 부족·검증 실패 등의 이유(재검색 프롬프트에 쓴다)
    regen_count: int  # 답변 재생성 횟수(상한 max_regen)
    research_count: int  # 재검색 횟수(상한 max_research)
    llm_calls: int  # 이번 질문이 쓴 LLM 호출 수(상한 max_llm_calls)


def reset_turn_update(question: str) -> dict:
    """새 질문 하나를 시작할 때 그래프에 넣는 입력. 지난 턴의 값이 남지 않도록 턴 단위 필드를 모두 비운다.

    - `original_question`에 이번 질문을 채우고, 카운터(regen_count, research_count, llm_calls)는 0으로 되돌린다.
    - 검색·답변 필드는 빈 값으로 되돌린다.
    - `tried_queries`는 reducer(operator.add)가 붙은 필드라서 `[]`를 넣으면 "빈 리스트를 더하기"가 되어 아무것도
      지워지지 않는다. 그래서 `Overwrite([])`로 reducer를 건너뛰고 값을 통째로 바꾼다.
    - `messages`(대화 이력)는 일부러 넣지 않는다. 지우면 이어지는 질문을 이해할 수 없다.

    새 턴은 `graph.invoke(reset_turn_update(질문), config)`처럼 **일반 dict**로 부른다. `Command(update=...)`로
    부르면 "최근 체크포인트에서 이어서 실행"하라는 뜻이 되어 새 턴이 아니라 지난 턴의 재개가 된다.
    """
    return {
        "original_question": question,
        "rewritten_query": "",
        "qclass": "",
        "tried_queries": Overwrite([]),
        "candidates": [],
        "reranked": [],
        "used_reranker": False,
        "sufficient": False,
        "answer": None,
        "violations": [],
        "verification": None,
        "failure_reason": None,
        "regen_count": 0,
        "research_count": 0,
        "llm_calls": 0,
    }
