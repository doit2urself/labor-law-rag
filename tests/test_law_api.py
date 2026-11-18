"""법제처 API 클라이언트(app/adapters/law_api.py)와 수집 스크립트(scripts/collect.py)의 동작을 검증한다.

네트워크는 쓰지 않는다. `httpx.MockTransport`로 가짜 응답을 돌려주고, 요청이 가이드의 요청 변수대로
나갔는지(주소·target·type·MST·ID 등)를 확인한다. 실제 응답 모양은 승인 뒤 Task 3 Step 6에서 받아 와 확인한다.
공식 가이드가 "항목 배열의 키 이름"을 알려 주지 않아서, 파서가 특정 키 이름에 기대지 않는지도 여기서 고정한다.
"""

import json

import httpx
import pytest

from app.adapters.law_api import LawApiClient
from app.domain.errors import LawApiAuthError, LawApiError
from scripts.collect import collect_interpretations, collect_laws, run_collect

OC = "TEST-OC-SECRET-123"


def make_client(handler) -> LawApiClient:
    """가짜 전송 계층을 끼운 클라이언트. 요청 간 대기는 0으로 해서 테스트가 느려지지 않게 한다."""
    return LawApiClient(OC, http=httpx.Client(transport=httpx.MockTransport(handler)), pause_s=0)


def json_response(payload: dict) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(payload, ensure_ascii=False).encode("utf-8"))


# 인증 실패는 JSON이 아니라 XML/HTML 본문으로 오기도 하므로, JSON 해석 전에 본문 문구부터 본다.
def test_auth_error_body_raises():
    body = "<Response><result>사용자 정보 검증에 실패하였습니다.</result></Response>"
    client = make_client(lambda request: httpx.Response(200, text=body))
    with pytest.raises(LawApiAuthError):
        client.search_laws("근로기준법")


# 오류 메시지에 인증값(OC)이 새면 로그·대화에 비밀이 남는다. 어떤 오류에서도 OC가 들어가면 안 된다.
def test_errors_never_contain_the_oc_value():
    for response in (httpx.Response(500, text="boom"), httpx.Response(200, text="<html>not json</html>")):
        client = make_client(lambda request, r=response: r)
        with pytest.raises(LawApiError) as info:
            client.search_laws("근로기준법")
        assert OC not in str(info.value)


# 법령 목록 요청은 가이드의 변수(OC, target=eflaw, type=JSON, query, nw, display, page)를 그대로 보낸다.
def test_search_laws_sends_guide_parameters():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return json_response({"LawSearch": {"totalCnt": "0", "page": "1"}})

    assert make_client(handler).search_laws("근로기준법", page=2, display=50) == []
    assert seen["path"].endswith("/DRF/lawSearch.do")
    assert seen["params"] == {
        "OC": OC, "target": "eflaw", "type": "JSON", "query": "근로기준법", "nw": "3", "display": "50", "page": "2",
    }


# current_only=False면 연혁까지 받으려고 nw를 보내지 않는다(가이드: 생략하면 전체).
def test_search_laws_without_current_only_omits_nw():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return json_response({"LawSearch": {"totalCnt": "0"}})

    make_client(handler).search_laws("근로기준법", current_only=False)
    assert "nw" not in seen["params"]


# 본문 조회는 MST와 시행일자(efYd, 가이드상 필수)를 보낸다.
def test_get_law_sends_mst_and_effective_date():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return json_response({"법령": {"기본정보": {"법령명_한글": "근로기준법"}}})

    out = make_client(handler).get_law("276291", ef_yd="20251023")
    assert out["법령"]["기본정보"]["법령명_한글"] == "근로기준법"
    assert seen["path"].endswith("/DRF/lawService.do")
    assert seen["params"] == {"OC": OC, "target": "eflaw", "type": "JSON", "MST": "276291", "efYd": "20251023"}


# 해석 본문 조회는 법령해석일련번호를 ID로 보낸다.
def test_get_interpretation_sends_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return json_response({"ExpcService": {"질의요지": "q"}})

    make_client(handler).get_interpretation("21822")
    assert seen["params"] == {"OC": OC, "target": "moelCgmExpc", "type": "JSON", "ID": "21822"}


# 해석 목록: 항목 두 개와 전체 건수(totalCnt)를 함께 돌려준다. 총 3건 중 첫 쪽(display=2)이다.
def test_search_interpretations_returns_items_and_total():
    rows = [{"법령해석일련번호": str(n)} for n in (1, 2, 3)]

    def handler(request: httpx.Request) -> httpx.Response:
        page, display = int(request.url.params["page"]), int(request.url.params["display"])
        chunk = rows[(page - 1) * display : page * display]
        return json_response({"CgmExpcMoelSearch": {"totalCnt": "3", "page": str(page), "cgmExpc": chunk}})

    items, total = make_client(handler).search_interpretations(page=1, display=2)
    assert len(items) == 2 and total == 3


# 결과가 한 건이면 배열이 아니라 객체 하나로 오는 API가 많다(XML→JSON 변환). 한 건도 목록으로 돌려준다.
def test_single_result_object_is_wrapped_into_a_list():
    client = make_client(lambda request: json_response({"Anything": {"totalCnt": "1", "item": {"법령해석일련번호": "7"}}}))
    items, total = client.search_interpretations()
    assert items == [{"법령해석일련번호": "7"}] and total == 1


# 결과가 없으면(totalCnt 0) 빈 목록이다.
def test_zero_results_gives_empty_list_and_zero_total():
    client = make_client(lambda request: json_response({"Anything": {"totalCnt": "0", "page": "1"}}))
    assert client.search_interpretations() == ([], 0)


# ---- 수집 스크립트 ----


class FakeLawApi:
    """수집 스크립트 시험용 가짜 클라이언트. 본문 조회(get_*) 횟수를 센다."""

    def __init__(self) -> None:
        self.get_calls = 0
        self.law_rows = [
            {"법령명한글": "근로기준법", "법령일련번호": "1", "법령ID": "10", "시행일자": "20250101"},
            {"법령명한글": "근로기준법 시행령", "법령일련번호": "2", "법령ID": "11", "시행일자": "20250101"},
        ]
        self.interp_rows = [{"법령해석일련번호": str(n)} for n in (101, 102, 103)]

    def search_laws(self, name, page=1, display=100, *, current_only=True):
        # 실제 API처럼 이름에 검색어가 들어 있는 법령을 모두 돌려준다(시행령·시행규칙도 함께 나온다).
        return [row for row in self.law_rows if name in row["법령명한글"]] if page == 1 else []

    def get_law(self, mst, ef_yd=None):
        self.get_calls += 1
        return {"MST": mst, "efYd": ef_yd}

    def search_interpretations(self, page=1, display=100):
        start = (page - 1) * display
        return self.interp_rows[start : start + display], len(self.interp_rows)

    def get_interpretation(self, interp_id):
        self.get_calls += 1
        return {"id": interp_id}


# 두 번 실행해도 본문 조회는 3번뿐이다(이미 저장된 파일은 다시 받지 않는다).
def test_collect_is_idempotent(tmp_path):
    api = FakeLawApi()
    first = run_collect("interpretations", api, tmp_path)
    second = run_collect("interpretations", api, tmp_path)
    assert api.get_calls == 3
    assert (first.saved, first.skipped, second.saved, second.skipped) == (3, 0, 0, 3)
    assert json.loads((tmp_path / "interpretations" / "101.json").read_text(encoding="utf-8")) == {"id": "101"}


# 목록이 여러 쪽이어도 전부 모은다(쪽 크기를 2로 줄여 3건이 2쪽에 걸치게 한다).
def test_collect_interpretations_follows_pages(tmp_path):
    api = FakeLawApi()
    report = collect_interpretations(api, tmp_path, page_size=2)
    assert report.saved == 3 and api.get_calls == 3


# 검색은 부분 일치라 "근로기준법"에 시행령도 따라온다. 이름이 정확히 같은 법령만 받고, 못 찾은 이름은 알려 준다.
def test_collect_laws_keeps_exact_names_only_and_reports_missing(tmp_path):
    api = FakeLawApi()
    report = collect_laws(api, tmp_path, names=["근로기준법", "없는법"])
    assert report.saved == 1 and report.missing == ["없는법"]
    assert (tmp_path / "laws" / "1.json").exists() and not (tmp_path / "laws" / "2.json").exists()
    # 본문 조회에는 목록이 알려 준 시행일자가 그대로 전달된다.
    assert json.loads((tmp_path / "laws" / "1.json").read_text(encoding="utf-8")) == {"MST": "1", "efYd": "20250101"}
    collect_laws(api, tmp_path, names=["근로기준법", "없는법"])
    assert api.get_calls == 1


# 인증값이 없으면 어떤 변수를 채워야 하는지 알려 주고 종료 코드 2로 끝난다(API를 부르지 않는다).
def test_main_without_oc_names_the_env_var(capsys):
    from scripts.collect import main

    assert main(["laws"], env={}) == 2
    assert "LABOR_LAW_API_OC" in capsys.readouterr().err


# 못 찾은 법령명이 있으면 종료 코드 3과 함께 그 이름을 보여 준다(app/scope.py를 고쳐야 한다는 신호).
def test_main_reports_missing_law_names(tmp_path, capsys):
    from scripts.collect import main

    code = main(["laws", "--out", str(tmp_path)], client=FakeLawApi())
    out = capsys.readouterr().out
    assert code == 3 and "missing=" in out and "근로자퇴직급여 보장법" in out
