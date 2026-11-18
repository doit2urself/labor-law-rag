"""SQLite 저장소(app/adapters/sqlite_store.py)의 동작을 검증한다.

가장 중요한 것은 두 가지다.
1. `test` 계정의 "같은 IP에서 5회" 제한이 **동시에 들어온 요청에서도 정확히 5번만** 성공하는가(확인과 증가가 쪼개지면 안 됨).
2. 상태가 파일에 남아서 저장소를 다시 열어도 유지되는가(서버가 재시작돼도 횟수가 초기화되면 안 됨).
"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.adapters.sqlite_store import AppStore, User


# 20개 스레드가 동시에 같은 IP로 시도해도 정확히 5번만 True다(확인+증가가 한 트랜잭션).
def test_quota_is_atomic_under_threads(tmp_path):
    store = AppStore(str(tmp_path / "a.db"))
    with ThreadPoolExecutor(20) as ex:
        results = list(ex.map(lambda _: store.try_consume_ip_quota("1.2.3.4", 5), range(20)))
    assert results.count(True) == 5


# 차감할 게 없는데 환불해도 0 아래로 내려가지 않는다(내려가면 한도가 늘어난다).
def test_refund_never_goes_negative(tmp_path):
    s = AppStore(str(tmp_path / "a.db"))
    s.refund_ip_quota("x")
    s.refund_ip_quota("x")
    assert s.try_consume_ip_quota("x", 1) is True
    assert s.try_consume_ip_quota("x", 1) is False  # 한도 1이므로 두 번째는 막힌다(환불이 음수로 쌓이지 않았다는 증거)


# 사용한 만큼 환불하면 다시 쓸 수 있다.
def test_refund_gives_back_one_use(tmp_path):
    s = AppStore(str(tmp_path / "a.db"))
    assert s.try_consume_ip_quota("x", 1) is True and s.try_consume_ip_quota("x", 1) is False
    s.refund_ip_quota("x")
    assert s.try_consume_ip_quota("x", 1) is True


# IP마다 따로 센다. 파일에 저장되므로 저장소를 다시 열어도(서버 재시작) 횟수가 남아 있다.
def test_ips_are_independent_and_state_survives_reopen(tmp_path):
    p = str(tmp_path / "a.db")
    s = AppStore(p)
    for _ in range(5):
        assert s.try_consume_ip_quota("a", 5)
    assert s.try_consume_ip_quota("a", 5) is False and s.try_consume_ip_quota("b", 5) is True
    assert AppStore(p).try_consume_ip_quota("a", 5) is False


# 리랭크 호출 수는 달("YYYY-MM")마다 따로 센다. incr는 올린 뒤의 값을 돌려준다.
def test_rerank_counter_is_per_month(tmp_path):
    s = AppStore(str(tmp_path / "a.db"))
    assert s.incr_rerank_calls("2026-11") == 1
    assert s.incr_rerank_calls("2026-11") == 2
    assert (s.get_rerank_calls("2026-11"), s.get_rerank_calls("2026-12")) == (2, 0)


# 20개 스레드가 동시에 올려도 하나도 잃지 않는다(월 한도 950회를 정확히 세려는 것).
def test_rerank_counter_is_atomic_under_threads(tmp_path):
    s = AppStore(str(tmp_path / "a.db"))
    with ThreadPoolExecutor(20) as ex:
        list(ex.map(lambda _: s.incr_rerank_calls("2026-11"), range(40)))
    assert s.get_rerank_calls("2026-11") == 40


# 계정: 저장하고 읽는다. 같은 이름으로 다시 저장하면 덮어쓴다(앱이 시작할 때마다 .env의 해시로 맞추기 때문).
def test_user_upsert_and_get(tmp_path):
    s = AppStore(str(tmp_path / "a.db"))
    assert s.get_user("admin") is None
    s.upsert_user("admin", "hash-1", "admin")
    assert s.get_user("admin") == User("admin", "hash-1", "admin")
    s.upsert_user("admin", "hash-2", "admin")
    assert s.get_user("admin").password_hash == "hash-2"


# 역할은 admin / test 둘뿐이다. 다른 값은 거절한다.
def test_user_role_is_restricted(tmp_path):
    s = AppStore(str(tmp_path / "a.db"))
    with pytest.raises(sqlite3.IntegrityError):
        s.upsert_user("x", "h", "root")  # type: ignore[arg-type]


# 저장소 파일이 들어갈 폴더(data/)가 아직 없어도 만들어 준다.
def test_creates_missing_parent_directory(tmp_path):
    AppStore(str(tmp_path / "data" / "app.db"))
    assert (tmp_path / "data" / "app.db").exists()
