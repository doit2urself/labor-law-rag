"""접속한 사람의 IP 읽기와 시도 횟수 제한.

역할:
- `client_ip`: 요청을 보낸 사람의 IP를 읽는다. 앞에 프록시(Caddy/Nginx)가 있으면 서버가 보는 접속 주소는 늘 프록시의 것이라,
  프록시가 붙여 주는 `X-Forwarded-For` 헤더를 읽어야 진짜 사용자를 알 수 있다. 단, 이 헤더는 **누구나 거짓으로 써서 보낼 수 있다**.
- `normalize_ip`: IPv6는 /64(한 가정·한 회선이 받는 묶음)를 한 사람으로 센다.
- `LoginThrottle`: 비밀번호를 연달아 틀리는 시도를 늦춘다.
사용처: app/api/routes.py (`test` 계정의 IP별 5회 제한, 로그인 시도 제한).

핵심 개념 — 위조된 X-Forwarded-For를 믿지 않는 법(스펙 6.6, 계획서 Review Focus 3):
`test` 계정은 "같은 IP에서 5회"라서, IP를 속일 수 있으면 제한이 무의미하다. 그래서 두 가지를 지킨다.
1. 접속 주소가 **신뢰하는 프록시**(설정 `trusted_proxies`, 기본 127.0.0.1)일 때만 헤더를 읽는다. 인터넷에서 곧바로 온 요청의
   헤더는 무시한다.
2. 헤더는 `클라이언트, 프록시1, 프록시2`처럼 왼쪽부터 쌓인다. 사용자가 맨 앞에 가짜 주소를 미리 써 보내도, 우리는 **오른쪽
   끝에서부터** 신뢰하는 프록시 주소를 걷어내고 처음 만나는 "신뢰할 수 없는 주소"를 쓴다. 그 주소는 우리가 믿는 프록시가
   직접 본 상대이므로 위조할 수 없다.
"""

from __future__ import annotations

import ipaddress
import threading
import time
from collections import deque
from collections.abc import Callable

from starlette.requests import Request

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# 접속 주소를 알 수 없을 때(드문 경우) 쓰는 값. 그런 요청은 한 칸으로 모아 센다.
UNKNOWN_IP = "unknown"


def _networks(entries: list[str]) -> list[IPNetwork]:
    """신뢰하는 프록시 목록을 네트워크로 바꾼다. 주소("127.0.0.1")도 대역("10.0.0.0/8")도 받는다."""
    return [ipaddress.ip_network(entry, strict=False) for entry in entries]


def validate_trusted_proxies(entries: list[str]) -> None:
    """목록에 주소가 아닌 값이 있으면 ValueError. 앱이 시작할 때 불러서, 오타가 요청이 올 때서야 500으로 드러나지 않게 한다."""
    for entry in entries:
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError as exc:
            raise ValueError(f"LABOR_TRUSTED_PROXIES에 주소가 아닌 값이 있습니다: {entry!r}") from exc


def _parse_ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _is_trusted(address: ipaddress.IPv4Address | ipaddress.IPv6Address, networks: list[IPNetwork]) -> bool:
    # IPv4 주소가 IPv6 대역에 "속하는지" 묻는 것은 오류가 아니라 그냥 False다(버전이 다르면 항상 False).
    return any(address in network for network in networks)


def client_ip(request: Request, trusted_proxies: list[str]) -> str:
    """요청을 보낸 사람의 IP를 문자열로 돌려준다(모듈 설명의 규칙).

    돌려주는 값이 주소가 아닐 수도 있다: 접속 주소를 아예 모르면 "unknown", 프록시가 이상한 값을 보내면 프록시 자신의 주소.
    둘 다 "가장 엄격한 칸"(여러 사람이 한 칸을 나눠 쓰는 쪽)으로 떨어지게 한 선택이다 — 모르면 느슨하게가 아니라 엄격하게.
    """
    peer = request.client.host if request.client else None
    if peer is None:
        return UNKNOWN_IP
    peer_address = _parse_ip(peer)
    networks = _networks(trusted_proxies)
    if peer_address is None or not _is_trusted(peer_address, networks):
        return peer  # 신뢰하지 않는 곳에서 온 요청: 헤더가 무엇이든 접속 주소가 곧 사용자다.

    # 헤더가 여러 줄로 나뉘어 올 수도 있으므로 모두 이어 붙인 뒤 쉼표로 가른다.
    entries = [e.strip() for e in ",".join(request.headers.getlist("x-forwarded-for")).split(",") if e.strip()]
    for entry in reversed(entries):
        address = _parse_ip(entry)
        if address is None:
            return peer  # 읽을 수 없는 값이 섞였으면 헤더 전체를 믿지 않는다.
        if not _is_trusted(address, networks):
            return entry
    return entries[0] if entries else peer  # 전부 신뢰 주소(예: 서버 안에서 직접 부른 경우): 가장 먼저 거친 주소


def normalize_ip(ip: str) -> str:
    """사용 횟수를 셀 때 쓰는 키로 바꾼다. IPv4는 그대로, IPv6는 /64 묶음("2001:db8:1:2::/64").

    IPv6는 한 가정이 /64 안의 주소를 사실상 마음대로(수없이 많이) 쓸 수 있어서, 주소 하나씩 세면 주소만 바꿔 제한을 우회한다.
    IPv4를 IPv6 모양으로 감싼 주소(::ffff:1.2.3.4)는 같은 사람의 IPv4와 같게 본다. 주소로 읽을 수 없는 값은 그대로 둔다.
    """
    address = _parse_ip(ip)
    if address is None:
        return ip
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((int(address), 64), strict=False))
    return str(address)


class LoginThrottle:
    """같은 키(보통 IP)에서 창(window_s초) 안에 실패가 max_failures번 쌓이면 잠시 막는 기억 장치.

    왜 필요한가: `admin`은 사용 횟수가 무제한이고 아이디도 공개돼 있어서, 비밀번호를 무한정 시도할 수 있으면 곤란하다.
    bcrypt는 한 번 확인에 시간이 걸려 시도 속도를 이미 늦추지만, 여기서 한 번 더 상한을 둔다.
    메모리에만 저장한다(서버는 워커 1개). 서버를 다시 시작하면 잊는다. 숫자(10회·15분)는 측정한 값이 아닌 시작값이다.
    clock: 시간을 바꿔 끼우는 자리(테스트용).
    """

    def __init__(
        self, max_failures: int = 10, window_s: float = 900.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._max = max_failures
        self._window = window_s
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()  # 요청이 여러 스레드에서 처리되므로 기록을 한 번에 하나씩만 고친다.

    def _recent(self, key: str) -> deque[float]:
        """창 밖으로 밀려난 옛 실패를 버리고 남은 기록을 돌려준다(호출하는 쪽이 잠금을 쥐고 있어야 한다)."""
        failures = self._failures.get(key)
        if failures is None:
            return deque()
        cutoff = self._clock() - self._window
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            del self._failures[key]
        return failures

    def is_blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key)) >= self._max

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._recent(key)  # 먼저 옛 기록을 치운다(없던 키는 만들지 않는다).
            self._failures.setdefault(key, deque()).append(self._clock())
