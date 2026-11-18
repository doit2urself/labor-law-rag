"""질문 하나가 쓸 수 있는 LLM 호출 예산 계산(상한 `Settings.max_llm_calls`, 기본 8회).

역할: "이 단계를 시작해도 상한을 넘지 않고 끝낼 수 있는가"를 한곳에서 판단한다.
사용처: 경로 함수(route_after_sufficiency, decide)와 재검색 노드(refine_node).

핵심 개념 — "지금 남은 호출 수"가 아니라 "이어질 단계까지 포함한 여유"를 본다:
답변 한 번은 생성(LLM 1회)과 검증(LLM 1회)이 항상 한 쌍이다. `llm_calls < 8`만 확인하고 생성을 시작하면, 7회째에
생성(8)한 뒤 검증(9)에서 상한을 넘는다. 검증을 못 하면 답변을 내보낼 수도 없으니(검증 통과가 출고 조건) 생성은 헛수고가 된다.
그래서 생성은 "생성 + 검증"이 모두 들어갈 때만 시작하고, 재검색은 "검색어 만들기 + 생성 + 검증"이 들어갈 때만 한다.
"""

from app.config import Settings

# 답변 한 번에 필요한 LLM 호출: 생성 1회 + 검증 1회.
ANSWER_CALLS = 2


def has_room(llm_calls: int, settings: Settings, needed: int) -> bool:
    """지금까지 `llm_calls`회를 썼을 때, 앞으로 `needed`회를 더 써도 상한(max_llm_calls) 이내인가."""
    return llm_calls + needed <= settings.max_llm_calls
