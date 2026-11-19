"""배포 점검 스크립트(scripts/verify_deploy.py)의 판정 로직을 시험한다 (Task 18 Step 7).

네트워크를 쓰는 부분은 실제 서버가 있어야 해서 시험하지 않고, "응답 코드 목록을 보고 합격/불합격을 가리는" 순수 함수만 시험한다.
이 판정이 틀리면 점검이 위조 헤더로 뚫린 서버를 "정상"이라고 말해 버리므로 중요하다.
"""

from __future__ import annotations

from scripts.verify_deploy import QUESTION_PLAN, judge_admin_run, judge_test_limit, parse_event_names


# 서버가 보낸 SSE 본문에서 이벤트 이름을 순서대로 뽑는다(데이터 줄의 글자는 이벤트 이름으로 읽지 않는다).
def test_parse_event_names_reads_only_event_lines():
    body = 'event: progress\ndata: {"label": "event: fake"}\n\nevent: answer\ndata: {}\n\n'
    assert parse_event_names(body) == ["progress", "answer"]


# 질문 계획: 7번 보내고, 그중 3번은 위조 헤더를 붙인다. 위조 헤더 값은 서로 달라야 한다(같으면 "한 칸에 쌓임"과 구별되지 않는다).
def test_question_plan_has_three_distinct_forged_headers():
    forged = [item for item in QUESTION_PLAN if item is not None]
    assert len(QUESTION_PLAN) == 7 and len(forged) == 3 and len(set(forged)) == 3


# 정상: 5번째까지 200, 그 뒤는 429. 위조 헤더가 붙은 7번째 요청도 막혀야 한다.
def test_limit_passes_when_first_five_ok_then_blocked():
    ok, _ = judge_test_limit([200, 200, 200, 200, 200, 429, 429], limit=5)
    assert ok


# 위조 헤더가 통했다면: 헤더 값이 다른 주소로 세어져 6번째·7번째가 막히지 않는다 → 불합격이고 메시지에 이유가 있다.
def test_limit_fails_when_forged_header_bypasses_the_block():
    ok, message = judge_test_limit([200, 200, 200, 200, 200, 200, 200], limit=5)
    assert not ok and "X-Forwarded-For" in message


# 7번째(위조 헤더 붙음)만 뚫린 경우도 불합격이다. 제한 뒤에 헤더로 우회하는 가장 단순한 공격이다.
def test_limit_fails_when_only_the_last_forged_request_gets_through():
    ok, _ = judge_test_limit([200, 200, 200, 200, 200, 429, 200], limit=5)
    assert not ok


# 5번째 이전에 막히면(예: 이미 횟수를 쓴 주소) 서버가 아니라 시험 조건이 틀린 것이다 → 불합격이고 원인을 알려 준다.
def test_limit_fails_when_blocked_too_early():
    ok, message = judge_test_limit([200, 200, 429, 429, 429, 429, 429], limit=5)
    assert not ok and "미리" in message


# 로그인 실패(401)나 서버 오류(500)가 섞이면 합격이 아니다.
def test_limit_fails_on_unexpected_status_codes():
    assert not judge_test_limit([401] * 7, limit=5)[0]
    assert not judge_test_limit([200, 200, 200, 200, 500, 429, 429], limit=5)[0]


# 관리자는 횟수 제한이 없다: 모든 요청이 200이어야 합격이다.
def test_admin_passes_only_when_every_request_is_answered():
    assert judge_admin_run([200] * 7)[0]
    assert not judge_admin_run([200, 200, 200, 200, 200, 200, 429])[0]
