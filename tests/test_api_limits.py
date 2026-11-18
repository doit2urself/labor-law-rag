"""접속 주소 읽기, `test` 계정의 IP별 사용 횟수, 로그인 시도 제한을 시험한다 (Task 14).

앞부분은 함수 단위 시험(IP 정규화, 프록시 헤더 해석, 시도 제한기), 뒷부분은 API를 통한 시험(5회 제한, 동시 요청, 위조 헤더)이다.
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.requests import Request

from app.api.limits import LoginThrottle, client_ip, normalize_ip
from tests.api_support import PEER_IP, login_cookie, make_app, make_client, post_chat
from tests.conftest import TEST_PASSWORD, FakeLlm

TRUSTED = ["127.0.0.1"]


def fake_request(peer: str | None, *forwarded_for: str) -> Request:
    """접속 주소(peer)와 X-Forwarded-For 헤더 줄들을 가진 요청 모양을 만든다(헤더 줄이 여럿일 수 있다)."""
    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", value.encode()) for value in forwarded_for],
        "client": (peer, 5000) if peer else None,
    }
    return Request(scope)


# ---- normalize_ip ----


# IPv4는 그대로 두고, IPv6는 같은 /64 안이면 같은 키가 되는지 확인한다(한 사용자가 /64 안에서 주소를 바꿔 우회하는 것을 막는다).
def test_normalize_ip_keeps_ipv4_and_groups_ipv6_by_64():
    assert normalize_ip("1.2.3.4") == "1.2.3.4"
    assert normalize_ip("2001:db8:1:2:aaaa:bbbb:cccc:dddd") == normalize_ip("2001:db8:1:2:1:2:3:4")
    assert normalize_ip("2001:db8:1:2::1") != normalize_ip("2001:db8:1:3::1")


# IPv4를 IPv6 모양으로 감싼 주소(::ffff:a.b.c.d)는 같은 사람의 IPv4와 같은 키여야 한다.
def test_normalize_ip_maps_ipv4_in_ipv6_to_ipv4():
    assert normalize_ip("::ffff:203.0.113.5") == "203.0.113.5"


# 주소로 읽을 수 없는 값은 오류를 내지 않고 그대로 돌려준다(그 값끼리 한 칸으로 센다).
def test_normalize_ip_returns_unparseable_text_unchanged():
    assert normalize_ip("unknown") == "unknown"


# ---- client_ip ----


# 신뢰하는 프록시가 아닌 곳에서 온 X-Forwarded-For는 위조일 수 있으므로 무시하고 접속 주소를 쓴다.
def test_forwarded_for_is_ignored_when_peer_is_not_trusted():
    assert client_ip(fake_request("203.0.113.9", "198.51.100.1"), TRUSTED) == "203.0.113.9"


# 프록시가 붙인 헤더는 따른다.
def test_forwarded_for_is_used_when_peer_is_trusted():
    assert client_ip(fake_request("127.0.0.1", "198.51.100.1"), TRUSTED) == "198.51.100.1"


# 클라이언트가 헤더 맨 앞에 가짜 주소를 미리 넣어 보내도, 오른쪽(프록시 쪽)에서부터 신뢰 가능한 주소를 걷어내고 처음 만나는
# 신뢰할 수 없는 주소를 쓰므로 가짜 주소가 이기지 못한다. CIDR 표기(10.0.0.0/8)도 신뢰 목록에 쓸 수 있다.
def test_rightmost_untrusted_address_wins_over_a_spoofed_leftmost_one():
    request = fake_request("127.0.0.1", "9.9.9.9, 198.51.100.1, 10.0.0.2")
    assert client_ip(request, ["127.0.0.1", "10.0.0.0/8"]) == "198.51.100.1"


# 헤더 줄이 여러 개로 나뉘어 와도 하나로 이어 붙여 읽는다.
def test_multiple_forwarded_for_header_lines_are_joined():
    request = fake_request("127.0.0.1", "9.9.9.9", "198.51.100.1")
    assert client_ip(request, TRUSTED) == "198.51.100.1"


# 헤더가 없거나, 주소가 아닌 값이 섞였거나, 전부 신뢰 주소인 경우의 마지막 안전선.
@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ((), "127.0.0.1"),  # 헤더 없음 → 프록시 자신의 주소
        (("not-an-ip",), "127.0.0.1"),  # 읽을 수 없는 값 → 프록시 자신의 주소(가장 엄격한 칸)
        (("127.0.0.1",), "127.0.0.1"),  # 전부 신뢰 주소 → 가장 왼쪽 주소
    ],
)
def test_client_ip_falls_back_safely(headers, expected):
    assert client_ip(fake_request("127.0.0.1", *headers), TRUSTED) == expected


# 접속 주소 자체를 알 수 없으면 "unknown"으로 모아 센다(예외로 요청이 죽지 않는다).
def test_client_ip_without_peer_is_unknown():
    assert client_ip(fake_request(None), TRUSTED) == "unknown"


# ---- LoginThrottle ----


# 같은 키가 창(window) 안에서 실패 한도에 닿으면 막히고, 다른 키는 영향을 받지 않는다.
def test_throttle_blocks_after_max_failures_per_key():
    throttle = LoginThrottle(max_failures=3, window_s=60, clock=lambda: 0.0)
    for _ in range(3):
        assert throttle.is_blocked("a") is False
        throttle.record_failure("a")
    assert throttle.is_blocked("a") is True
    assert throttle.is_blocked("b") is False


# 시간이 창보다 더 지나면 옛 실패는 잊힌다(영원히 잠기지 않는다).
def test_throttle_forgets_failures_after_the_window():
    now = {"t": 0.0}
    throttle = LoginThrottle(max_failures=2, window_s=60, clock=lambda: now["t"])
    throttle.record_failure("a")
    throttle.record_failure("a")
    assert throttle.is_blocked("a") is True
    now["t"] = 61.0
    assert throttle.is_blocked("a") is False


# ---- API: test 계정의 IP별 5회 제한 ----


# admin은 30번을 연달아 질문해도 모두 통과한다(사용 횟수 제한이 없다).
async def test_admin_is_unlimited(api_app, admin_cookie):
    for _ in range(30):
        assert (await post_chat(api_app, admin_cookie)).status_code == 200


# test는 같은 IP에서 5번까지 통과하고 6번째는 429와 안내 문구를 받는다.
async def test_test_user_gets_five_then_message(api_app, test_cookie):
    codes = [(await post_chat(api_app, test_cookie)).status_code for _ in range(6)]
    assert codes == [200] * 5 + [429]
    over = await post_chat(api_app, test_cookie)
    assert over.json()["detail"] == "이 IP의 사용 횟수(5회)를 초과했습니다."


# 한도를 넘긴 요청은 그래프를 돌리지 않는다(무료 LLM 호출을 쓰지 않는다).
async def test_over_the_limit_request_does_not_run_the_graph(tmp_path):
    llm = FakeLlm.script()
    app = make_app(tmp_path, llm=llm, test_ip_limit=1)
    cookie = await login_cookie(app, "test", TEST_PASSWORD)
    assert (await post_chat(app, cookie)).status_code == 200
    calls_after_first = llm.call_count
    assert (await post_chat(app, cookie)).status_code == 429
    assert llm.call_count == calls_after_first


# 같은 IP에서 동시에 열 건이 들어와도 정확히 5건만 통과한다(확인과 증가가 한 트랜잭션이라서).
async def test_concurrent_requests_from_one_ip_allow_exactly_five(api_app, test_cookie):
    responses = await asyncio.gather(*[post_chat(api_app, test_cookie) for _ in range(10)])
    assert sorted(r.status_code for r in responses).count(200) == 5


# 신뢰하지 않는 곳에서 온 X-Forwarded-For는 무시되므로, 헤더를 바꿔 가며 보내도 한 칸으로 세어진다.
async def test_spoofed_forwarded_for_is_ignored(api_app, test_cookie):
    codes = [
        (
            await post_chat(
                api_app, test_cookie, peer=("203.0.113.9", 5000), headers={"X-Forwarded-For": f"198.51.100.{i}"}
            )
        ).status_code
        for i in range(6)
    ]
    assert codes == [200] * 5 + [429]


# 신뢰하는 프록시(127.0.0.1)가 붙인 헤더는 따르고, 프록시가 알려 준 다른 클라이언트는 따로 센다.
async def test_forwarded_for_is_honored_only_from_trusted_proxy(api_app, test_cookie):
    async def chat(xff: str) -> int:
        response = await post_chat(api_app, test_cookie, peer=("127.0.0.1", 5000), headers={"X-Forwarded-For": xff})
        return response.status_code

    assert [await chat("198.51.100.1") for _ in range(6)] == [200] * 5 + [429]
    assert await chat("198.51.100.2") == 200


# 빈 질문·공백뿐인 질문·너무 긴 질문은 422로 거절하고 사용 횟수를 차감하지 않는다.
@pytest.mark.parametrize("question", ["", "   ", "가" * 1001])
async def test_invalid_questions_rejected_and_not_counted(api_app, test_cookie, question):
    assert (await post_chat(api_app, test_cookie, question)).status_code == 422
    assert api_app.state.store.try_consume_ip_quota(PEER_IP, 5) is True  # 차감되지 않았으니 첫 사용이 성공한다


# 정확히 1,000자는 허용한다(경계값).
async def test_question_of_exactly_the_maximum_length_is_accepted(api_app, test_cookie):
    assert (await post_chat(api_app, test_cookie, "가" * 1000)).status_code == 200


# ---- API: 로그인 시도 제한 ----


# 비밀번호를 열 번 틀리면, 그 다음에는 맞는 비밀번호로도 로그인할 수 없다(무차별 대입을 늦춘다).
async def test_login_is_throttled_after_repeated_failures(api_app):
    async with make_client(api_app) as client:
        for _ in range(10):
            assert (await client.post("/api/login", json={"username": "admin", "password": "wrong"})).status_code == 401
        blocked = await client.post("/api/login", json={"username": "test", "password": TEST_PASSWORD})
    assert blocked.status_code == 429
    assert "너무 많" in blocked.json()["detail"]
