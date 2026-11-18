"""법제처 국가법령정보 공동활용 Open API 클라이언트.

역할: 현행 법령 본문(`target=eflaw`)과 고용노동부 법령해석(`target=moelCgmExpc`)의 목록·본문을 가져온다.
사용처: scripts/collect.py(수집 스크립트). 서비스(API 서버)는 이 클라이언트를 쓰지 않는다 — 스펙 6.1에 따라
법제처 API는 데이터를 모을 때만 부르고, 받은 JSON을 서버에 보관해서 쓴다.
입력: 인증값 OC(법제처가 승인 때 준 값). 출력: 목록은 (항목 dict의 목록, 전체 건수), 본문은 응답 JSON 그대로.

요청 변수와 응답 필드는 공식 가이드(open.law.go.kr 의 lsEfYdListGuide, lsEfYdInfoGuide,
cgmExpcMoelListGuide, cgmExpcMoelInfoGuide)에서 읽은 것이다. 가이드가 알려 주지 않는 부분(항목 배열의 키 이름)은
특정 이름을 추측하지 않고 응답 모양에서 찾는다 → `_split_search_payload`. 실제 응답과 맞는지는 승인 뒤
Task 3 Step 6에서 확인한다.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from app.domain.errors import LawApiAuthError, LawApiError

# 인증 실패 때 본문에 들어 있는 문구(계획 Task 3). 승인 전이거나 등록한 서버 IP가 아닐 때 나온다.
AUTH_FAIL_MESSAGE = "사용자 정보 검증에 실패하였습니다"

# 목록 항목에서 읽는 필드 이름(가이드 표기 그대로). 수집 스크립트가 파일 이름과 본문 조회 인자를 만들 때 쓴다.
KEY_LAW_NAME = "법령명한글"
KEY_LAW_MST = "법령일련번호"
KEY_LAW_EF_YD = "시행일자"
KEY_INTERP_ID = "법령해석일련번호"

# 목록 응답의 머리 정보(항목이 아닌 값). 이 이름들은 "항목 배열"을 찾을 때 건너뛴다.
_META_KEYS = frozenset({"target", "키워드", "section", "totalCnt", "page", "resultCode", "resultMsg"})


def _to_int(value: Any) -> int:
    """`"3"`, `3`, `"1,200"`처럼 오는 숫자를 정수로 바꾼다. 못 바꾸면 0."""
    try:
        return int(str(value).replace(",", "").strip())
    except ValueError:
        return 0


def _split_search_payload(payload: dict[str, Any]) -> tuple[list[dict], int]:
    """목록 응답에서 (항목 목록, 전체 건수)를 뽑는다.

    왜 키 이름을 쓰지 않나: 가이드는 `totalCnt`·`page`는 알려 주지만, 항목들이 담긴 배열의 키 이름은 알려 주지 않는다.
    그래서 "머리 정보가 아닌 첫 목록(또는 객체)"을 항목으로 본다. 응답이 한 겹 감싸여 있으면(`{"LawSearch": {...}}`)
    그 안쪽을 본다. 결과가 한 건이면 배열이 아니라 객체 하나로 오는 API가 많아서, 객체도 한 건짜리 목록으로 바꾼다.
    """
    body: dict[str, Any] = payload
    if len(payload) == 1:
        only = next(iter(payload.values()))
        if isinstance(only, dict):
            body = only

    total = _to_int(body.get("totalCnt", 0))
    for key, value in body.items():
        if key in _META_KEYS:
            continue
        if isinstance(value, list) and all(isinstance(row, dict) for row in value):
            return list(value), total
        if isinstance(value, dict):
            return [value], total
    return [], total


class LawApiClient:
    """법제처 공동활용 API를 부르는 얇은 클라이언트. 재시도 없이 단순 호출만 한다(계획 Task 3 Step 4).

    oc: 법제처가 승인 때 준 인증값. 어떤 오류 메시지에도 넣지 않는다(로그·대화에 비밀이 남지 않게).
    http: 테스트에서 `httpx.MockTransport`를 끼운 클라이언트를 넣는 자리. 없으면 시간 제한 30초짜리를 만든다.
    pause_s: 요청 사이에 쉬는 시간. 법제처 서버에 부담을 주지 않으려고 0.2초(계획)로 둔다.
    """

    def __init__(
        self,
        oc: str,
        http: httpx.Client | None = None,
        base: str = "https://www.law.go.kr/DRF",
        *,
        pause_s: float = 0.2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._oc = oc
        self._http = http if http is not None else httpx.Client(timeout=30.0)
        self._base = base.rstrip("/")
        self._pause_s = pause_s
        self._sleep = sleep

    def _redact(self, text: str) -> str:
        """메시지에 인증값이 섞여 있으면 가린다(오류 페이지가 요청 주소를 되풀이하는 경우를 대비)."""
        return text.replace(self._oc, "***") if self._oc else text

    def _request(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        """한 번 호출하고 JSON(dict)을 돌려준다. 실패는 모두 LawApiError 계열로 바꾼다."""
        if self._pause_s > 0:
            self._sleep(self._pause_s)
        query = {"OC": self._oc, "type": "JSON", **params}
        try:
            response = self._http.get(f"{self._base}/{endpoint}", params=query)
        except httpx.HTTPError as exc:
            # `from None`: 원래 예외의 메시지에는 요청 주소(= OC 값)가 들어 있어서 연결하지 않는다.
            raise LawApiError(f"법제처 API에 연결하지 못했습니다({type(exc).__name__})") from None

        # 인증 실패는 JSON이 아니라 XML/HTML 본문으로 오기도 한다. 그래서 JSON 해석보다 먼저 문구를 본다.
        if AUTH_FAIL_MESSAGE in response.text:
            raise LawApiAuthError("법제처 API가 인증을 거부했습니다. 승인 상태와 등록한 서버 IP를 확인하세요.")
        if response.status_code != 200:
            raise LawApiError(f"법제처 API가 HTTP {response.status_code}를 돌려줬습니다")
        try:
            payload = response.json()
        except ValueError:
            snippet = self._redact(response.text[:100])
            raise LawApiError(f"법제처 API 응답이 JSON이 아닙니다: {snippet!r}") from None
        if not isinstance(payload, dict):
            raise LawApiError("법제처 API 응답의 최상위가 JSON 객체가 아닙니다")
        return payload

    def search_laws(self, name: str, page: int = 1, display: int = 100, *, current_only: bool = True) -> list[dict]:
        """법령명 검색(`lawSearch.do`, `target=eflaw`). 이름에 `name`이 들어 있는 법령을 모두 돌려준다.

        current_only: 참이면 `nw=3`(현행)만 받는다. 가이드: `nw`를 생략하면 연혁·시행예정까지 전부 오므로
        검색에 옛 버전이 섞이지 않게 기본을 현행으로 둔다(계획의 시그니처에 없던 인자 — 원장에 판단 기록).
        검색은 부분 일치라 "근로기준법"에 시행령·시행규칙도 따라온다.
        """
        params: dict[str, Any] = {"target": "eflaw", "query": name, "display": display, "page": page}
        if current_only:
            params["nw"] = 3
        items, _total = _split_search_payload(self._request("lawSearch.do", params))
        return items

    def get_law(self, mst: str, ef_yd: str | None = None) -> dict[str, Any]:
        """법령 본문 조회(`lawService.do`, `target=eflaw`). 응답 JSON을 그대로 돌려준다.

        mst: 법령일련번호(가이드의 MST). ef_yd: 시행일자. 가이드가 `efYd`를 필수로 적었으므로 목록이 알려 준
        시행일자를 그대로 넘긴다(계획의 시그니처에 없던 인자). 생략 가능하게 둔 것은 가이드대로 동작하는지
        실제 응답으로 확인하기 전이라서다.
        """
        params: dict[str, Any] = {"target": "eflaw", "MST": mst}
        if ef_yd is not None:
            params["efYd"] = ef_yd
        return self._request("lawService.do", params)

    def search_interpretations(self, page: int = 1, display: int = 100) -> tuple[list[dict], int]:
        """고용노동부 법령해석 목록(`lawSearch.do`, `target=moelCgmExpc`). (항목 목록, 전체 건수)를 돌려준다."""
        params = {"target": "moelCgmExpc", "display": display, "page": page}
        return _split_search_payload(self._request("lawSearch.do", params))

    def get_interpretation(self, interp_id: str) -> dict[str, Any]:
        """고용노동부 법령해석 본문(`lawService.do`, `target=moelCgmExpc`). `ID`는 법령해석일련번호다."""
        return self._request("lawService.do", {"target": "moelCgmExpc", "ID": interp_id})
