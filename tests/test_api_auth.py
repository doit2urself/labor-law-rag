"""비밀번호 해시, 세션 쿠키, 로그인·로그아웃·내 정보 API, 시작할 때의 설정 검증을 시험한다 (Task 14)."""

from __future__ import annotations

import bcrypt
import pytest

from app.api.auth import SESSION_COOKIE, SessionCodec, hash_password, looks_like_bcrypt, verify_password
from tests.api_support import make_app, make_client
from tests.conftest import ADMIN_PASSWORD, TEST_PASSWORD, settings_factory

# ---- 비밀번호 해시 ----


# 만든 해시로 같은 비밀번호만 통과하고, 해시에는 비밀번호 원문이 들어 있지 않다.
def test_hash_password_verifies_only_the_same_password():
    hashed = hash_password("pw-for-test", rounds=4)
    assert verify_password("pw-for-test", hashed) is True
    assert verify_password("wrong", hashed) is False
    assert "pw-for-test" not in hashed


# 같은 비밀번호라도 해시는 매번 다르다(소금이 매번 새로 뽑히므로 해시 목록으로 비밀번호를 알아낼 수 없다).
def test_hash_password_is_salted():
    assert hash_password("same", rounds=4) != hash_password("same", rounds=4)


# bcrypt는 72바이트까지만 읽는다. 해시를 만들 때 넘기면 조용히 잘라 쓰지 않고 거절한다(한글은 글자당 3바이트).
def test_hash_password_rejects_more_than_72_bytes():
    with pytest.raises(ValueError, match="72"):
        hash_password("가" * 25, rounds=4)


# 너무 긴 비밀번호나 깨진 해시를 받아도 예외로 요청이 죽지 않고 "불일치"로 답한다(bcrypt 5.0은 ValueError를 낸다).
def test_verify_password_returns_false_for_bad_input():
    good = hash_password("x", rounds=4)
    assert verify_password("가" * 25, good) is False
    assert verify_password("x", "not-a-bcrypt-hash") is False


# bcrypt 해시 모양만 해시로 인정하고, 가짜 문자열·빈 값은 거른다.
def test_looks_like_bcrypt():
    assert looks_like_bcrypt(hash_password("x", rounds=4)) is True
    assert looks_like_bcrypt("test-admin-hash") is False
    assert looks_like_bcrypt("") is False


# ---- 세션 쿠키 ----


# 쿠키로 만든 값을 되돌리면 같은 사용자 이름이 나온다.
def test_session_roundtrip_returns_the_username():
    codec = SessionCodec("secret")
    assert codec.loads(codec.dumps("test")) == "test"


# 서명이 어긋난 값(다른 비밀키로 만든 것, 고쳐 쓴 것, 아무 문자열, 없음)은 모두 "로그인 안 함"이다.
def test_session_rejects_forged_or_garbage_tokens():
    token = SessionCodec("secret").dumps("admin")
    assert SessionCodec("other-secret").loads(token) is None
    assert SessionCodec("secret").loads(token[:-2] + "xx") is None
    assert SessionCodec("secret").loads("garbage") is None
    assert SessionCodec("secret").loads(None) is None


# 유효 기간이 지난 쿠키는 쓸 수 없다(시계를 바꿔 끼워 시험한다).
def test_session_expires():
    now = {"t": 1_000.0}
    codec = SessionCodec("secret", max_age_s=60, now=lambda: now["t"])
    token = codec.dumps("test")
    now["t"] += 60
    assert codec.loads(token) == "test"  # 정확히 60초는 아직 유효
    now["t"] += 1
    assert codec.loads(token) is None


# ---- 로그인·로그아웃·내 정보 ----


# 생성한 해시로 로그인되고 틀린 비밀번호는 401이다.
async def test_login_with_generated_hash_and_wrong_password(tmp_path):
    app = make_app(tmp_path, test_password_hash=hash_password("pw-for-test", rounds=4))
    async with make_client(app) as client:
        ok = await client.post("/api/login", json={"username": "test", "password": "pw-for-test"})
        assert ok.status_code == 200
        assert ok.json() == {"username": "test", "role": "test"}
        bad = await client.post("/api/login", json={"username": "test", "password": "wrong"})
        assert bad.status_code == 401


# 로그인 쿠키는 스크립트가 못 읽고(HttpOnly), HTTPS로만 가고(Secure), 다른 사이트에서 시작한 요청에는 안 붙는다(SameSite).
async def test_login_cookie_has_protective_attributes(api_app):
    async with make_client(api_app) as client:
        response = await client.post("/api/login", json={"username": "admin", "password": ADMIN_PASSWORD})
    set_cookie = response.headers["set-cookie"].lower()
    assert SESSION_COOKIE in set_cookie
    assert "httponly" in set_cookie and "secure" in set_cookie and "samesite=lax" in set_cookie
    assert ADMIN_PASSWORD not in response.text and ADMIN_PASSWORD not in set_cookie


# 없는 계정과 틀린 비밀번호는 같은 응답이다(어느 쪽이 틀렸는지 알려 주지 않는다).
async def test_unknown_user_and_wrong_password_look_the_same(api_app):
    async with make_client(api_app) as client:
        wrong_password = await client.post("/api/login", json={"username": "admin", "password": "nope"})
        unknown_user = await client.post("/api/login", json={"username": "nobody", "password": "nope"})
    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json()


# 없는 계정으로 시도해도 bcrypt 비교를 한 번 한다(응답 시간 차이로 "이 아이디는 있다"가 새지 않게). 시간을 재는 대신,
# 비교 함수가 실제 bcrypt 해시와 호출됐는지로 확인한다.
async def test_unknown_user_still_costs_one_real_bcrypt_comparison(api_app, monkeypatch):
    import app.api.routes as routes

    seen_hashes: list[str] = []
    real_verify = routes.verify_password

    def spy(plain: str, hashed: str) -> bool:
        seen_hashes.append(hashed)
        return real_verify(plain, hashed)

    monkeypatch.setattr(routes, "verify_password", spy)
    async with make_client(api_app) as client:
        await client.post("/api/login", json={"username": "nobody", "password": "nope"})
    assert len(seen_hashes) == 1 and looks_like_bcrypt(seen_hashes[0])


# 로그인 없이는 /api/me가 401이고, 로그인하면 이름과 역할을 돌려준다.
async def test_me_requires_login_and_returns_role(api_app, test_cookie):
    async with make_client(api_app) as anonymous:
        assert (await anonymous.get("/api/me")).status_code == 401
    async with make_client(api_app, test_cookie) as client:
        assert (await client.get("/api/me")).json() == {"username": "test", "role": "test"}


# 로그아웃하면 쿠키가 지워져서 이후 요청이 401이 된다.
async def test_logout_clears_the_session(api_app):
    async with make_client(api_app) as client:
        await client.post("/api/login", json={"username": "admin", "password": ADMIN_PASSWORD})
        assert (await client.get("/api/me")).status_code == 200
        assert (await client.post("/api/logout")).status_code == 200
        assert (await client.get("/api/me")).status_code == 401


# 위조한 쿠키로는 보호된 API를 쓸 수 없다.
async def test_forged_cookie_is_rejected(api_app):
    forged = SessionCodec("not-the-real-secret").dumps("admin")
    async with make_client(api_app, forged) as client:
        assert (await client.get("/api/me")).status_code == 401
        assert (await client.post("/api/chat", json={"question": "q"})).status_code == 401


# 형식이 틀린 요청은 422이고, 화면이 그대로 보여 줄 수 있도록 detail이 목록이 아니라 문장이다.
async def test_malformed_login_body_gives_422_with_text_detail(api_app):
    async with make_client(api_app) as client:
        response = await client.post("/api/login", json={"username": "admin"})
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)


# 서버는 로그인 없이도 살아 있는지 확인할 수 있어야 한다.
async def test_health_needs_no_login(api_app):
    async with make_client(api_app) as client:
        response = await client.get("/api/health")
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


# ---- 시작할 때의 설정 검증과 계정 동기화 ----


# 시작할 때 .env의 해시로 두 계정이 DB에 들어간다. 해시를 바꿔 다시 시작하면 DB의 해시도 바뀐다(비밀번호 교체 방법).
def test_startup_syncs_accounts_from_settings(tmp_path):
    app = make_app(tmp_path)
    assert app.state.store.get_user("admin").role == "admin"
    assert app.state.store.get_user("test").role == "test"
    new_hash = hash_password("new-pw", rounds=4)
    restarted = make_app(tmp_path, test_password_hash=new_hash)
    assert restarted.state.store.get_user("test").password_hash == new_hash


# 해시 형식이 아닌 값(예: 실수로 비밀번호 원문을 넣음)이면 로그인이 조용히 안 되는 채로 도는 대신 시작할 때 멈춘다.
# 오류 메시지에 그 값은 담지 않는다(비밀이 로그에 남지 않게).
def test_startup_fails_fast_on_a_non_bcrypt_hash(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        make_app(tmp_path, admin_password_hash="hunter2-plain-text")
    assert "LABOR_ADMIN_PASSWORD_HASH" in str(excinfo.value)
    assert "hunter2-plain-text" not in str(excinfo.value)


# 신뢰 프록시 목록에 주소가 아닌 값이 있으면(오타) 시작할 때 멈춘다. 요청이 올 때서야 500이 나는 것보다 낫다.
def test_startup_fails_fast_on_a_bad_trusted_proxy(tmp_path):
    with pytest.raises(ValueError, match="LABOR_TRUSTED_PROXIES"):
        make_app(tmp_path, trusted_proxies=["127.0.0.1", "not-an-address"])


# 테스트 공용 설정의 해시가 진짜 bcrypt 해시라서 앱 시작 검사를 통과한다.
def test_default_settings_hashes_are_real_bcrypt_hashes():
    s = settings_factory()
    assert looks_like_bcrypt(s.admin_password_hash) and looks_like_bcrypt(s.test_password_hash)
    assert bcrypt.checkpw(TEST_PASSWORD.encode(), s.test_password_hash.encode())
