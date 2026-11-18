"""로그인: 비밀번호 해시, 세션 쿠키, 요청에서 현재 사용자 찾기.

역할: 계정 두 개(`admin`, `test`)의 비밀번호를 안전하게 확인하고, 로그인한 사람에게 서명된 쿠키를 준다.
사용처: app/api/routes.py (로그인·보호된 API), scripts/make_password_hash.py (해시 만들기), app/api/main.py (시작 검사).

핵심 개념 1 — 비밀번호는 원문이 아니라 bcrypt 해시로만 둔다:
서버 설정(.env)에는 비밀번호 대신 해시만 있다. bcrypt는 일부러 느리고(비용 12 ≈ 0.2초), 해시마다 새 소금(salt)이 섞여서
같은 비밀번호도 해시가 다르다. 그래서 설정 파일이 새도 비밀번호를 곧바로 알 수 없고, 무차별 대입도 느려진다.

핵심 개념 2 — 세션 쿠키는 "서명된 이름표"다:
로그인하면 `{"u": 사용자 이름, "iat": 발급 시각}`을 서버 비밀키(`session_secret`)로 서명해 쿠키에 넣는다. 서버는 DB에 세션을
저장하지 않고, 돌아온 쿠키의 서명이 맞는지만 확인한다. 서명은 비밀키 없이는 만들 수 없으므로 위조할 수 없다(itsdangerous).
쿠키 속성: HttpOnly(화면의 스크립트가 못 읽음 → XSS로 훔치기 어려움), Secure(HTTPS에서만 전송), SameSite=Lax(다른 사이트에서
시작된 POST에는 붙지 않음 → CSRF 방어). 서버는 쿠키의 이름만 믿지 않고 매번 DB에서 계정을 다시 읽어 역할(role)을 정한다.
"""

from __future__ import annotations

import functools
import re
import time
from collections.abc import Callable

import bcrypt
from fastapi import HTTPException, Request
from itsdangerous import BadData, URLSafeSerializer

from app.adapters.sqlite_store import User
from app.texts import LOGIN_REQUIRED_MESSAGE

SESSION_COOKIE = "labor_session"

# 로그인 유효 기간. 공개 서비스라 짧게 잡은 시작값(24시간)이며 측정한 값이 아니다. 지나면 다시 로그인한다.
SESSION_MAX_AGE_S = 24 * 60 * 60

# bcrypt가 읽는 비밀번호의 최대 길이. 이보다 길면 뒷부분이 해시에 반영되지 않는다(bcrypt 5.0은 아예 ValueError를 낸다).
MAX_PASSWORD_BYTES = 72

# bcrypt 해시의 모양: `$2b$` + 비용 두 자리 + `$` + 소금 22자 + 해시 31자(합쳐서 60자).
_BCRYPT_PATTERN = re.compile(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}")


def hash_password(plain: str, *, rounds: int = 12) -> str:
    """비밀번호의 bcrypt 해시를 만든다. rounds는 비용(2의 거듭제곱 횟수)이며 1 올릴 때마다 시간이 두 배다(테스트는 4를 쓴다).

    72바이트를 넘으면 조용히 자르지 않고 ValueError를 낸다(한글은 글자당 3바이트라 24자 정도가 한계).
    """
    raw = plain.encode()
    if len(raw) > MAX_PASSWORD_BYTES:
        raise ValueError(f"비밀번호는 {MAX_PASSWORD_BYTES}바이트(한글은 약 24자)를 넘을 수 없습니다")
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """비밀번호가 해시와 맞는지. 너무 긴 비밀번호나 깨진 해시처럼 bcrypt가 ValueError를 내는 입력은 예외 대신 False다."""
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:
        return False


def looks_like_bcrypt(value: str) -> bool:
    """값이 bcrypt 해시 모양인지. 비밀번호 원문을 해시 자리에 잘못 넣은 설정을 시작할 때 걸러 내는 데 쓴다."""
    return _BCRYPT_PATTERN.fullmatch(value) is not None


@functools.cache
def dummy_hash() -> str:
    """없는 계정으로 로그인을 시도할 때 대신 비교할 해시.

    왜 필요한가: 없는 계정이면 곧바로 거절하고 있는 계정이면 bcrypt(0.2초)를 돌리면, 응답 시간 차이로 "이 아이디는 있다"는
    사실이 새어 나간다. 없는 계정도 같은 비용의 비교를 한 번 하게 해서 시간을 맞춘다. 처음 쓸 때 한 번만 만든다.
    """
    return hash_password("dummy-password-for-timing")


class SessionCodec:
    """사용자 이름을 서명된 쿠키 값으로 바꾸고(dumps), 쿠키 값에서 되돌린다(loads).

    secret: 서명 비밀키(설정 `session_secret`). 바꾸면 이미 나간 쿠키가 전부 무효가 된다.
    max_age_s: 발급 뒤 유효한 시간(초). now: 시계를 바꿔 끼우는 자리(테스트용).
    """

    def __init__(self, secret: str, max_age_s: int = SESSION_MAX_AGE_S, now: Callable[[], float] = time.time) -> None:
        # salt는 같은 비밀키로 만든 다른 용도의 서명과 섞이지 않게 하는 이름표다.
        self._serializer = URLSafeSerializer(secret, salt="labor-rag-session")
        self._max_age = max_age_s
        self._now = now

    def dumps(self, username: str) -> str:
        return self._serializer.dumps({"u": username, "iat": int(self._now())})

    def loads(self, token: str | None) -> str | None:
        """쿠키 값이 우리가 서명했고 유효 기간 안이면 사용자 이름, 아니면 None(로그인 안 한 것으로 본다)."""
        if not token:
            return None
        try:
            data = self._serializer.loads(token)
        except BadData:  # 서명이 틀렸거나 형식이 깨진 모든 경우
            return None
        if not isinstance(data, dict):
            return None
        username, issued_at = data.get("u"), data.get("iat")
        if not isinstance(username, str) or not isinstance(issued_at, int):
            return None
        if self._now() - issued_at > self._max_age:
            return None
        return username


def current_user(request: Request) -> User:
    """FastAPI 의존성: 요청의 세션 쿠키로 로그인한 계정을 찾는다. 없거나 위조·만료면 401.

    쿠키에 역할을 담지 않고 매번 DB에서 읽으므로, 계정을 지우거나 역할을 바꾸면 이미 나간 쿠키에도 바로 반영된다.
    """
    username = request.app.state.sessions.loads(request.cookies.get(SESSION_COOKIE))
    user = request.app.state.store.get_user(username) if username else None
    if user is None:
        raise HTTPException(status_code=401, detail=LOGIN_REQUIRED_MESSAGE)
    return user
