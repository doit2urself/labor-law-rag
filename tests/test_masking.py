"""개인정보 마스킹(app/observability/masking.py)을 시험한다 (Task 16).

마스킹은 Langfuse에 보내는 **기록**에만 적용한다. LLM에 보내는 입력은 건드리지 않는다(그 시험은 test_langfuse_wiring.py).
"""

import json

import pytest

from app.observability.masking import mask_pii, mask_serialized, mask_value


# 주민등록번호·휴대전화·이메일이 각자의 표시로 바뀐다.
def test_mask_pii():
    assert mask_pii("주민번호 900101-1234567 입니다") == "주민번호 [주민번호] 입니다"
    assert mask_pii("연락처 010-1234-5678") == "연락처 [전화번호]"
    assert mask_pii("a.b@c.com 로 보내줘") == "[이메일] 로 보내줘"


# 법령 문장과 금액·시간 숫자는 그대로 둔다(마스킹이 질문의 뜻을 망가뜨리면 기록을 읽을 수 없다).
def test_mask_leaves_legal_text_alone():
    s = "근로기준법 제55조에 따라 주 15시간 이상 근무하면 1,000원"
    assert mask_pii(s) == s


# 같은 번호라도 쓰는 방식이 여러 가지다: 하이픈 없음, 공백·점 구분, +82 국제 표기.
@pytest.mark.parametrize(
    "written",
    ["01012345678", "010 1234 5678", "010.1234.5678", "011-123-4567", "+82 10-1234-5678", "+82-10-1234-5678"],
)
def test_mask_phone_variants(written):
    assert mask_pii(f"번호는 {written} 입니다") == "번호는 [전화번호] 입니다"


# 주민등록번호도 하이픈이 없거나 공백으로 나뉜 채 쓰일 수 있다. 앞자리가 010101(2001-01-01생)처럼 전화번호와 닮아도 주민번호로 가린다.
@pytest.mark.parametrize("written", ["9001011234567", "900101 1234567", "010101-3234567"])
def test_mask_rrn_variants(written):
    assert mask_pii(f"번호 {written} 끝") == "번호 [주민번호] 끝"


# 뒷자리 첫 숫자가 1~8이 아닌 13자리(0 또는 9로 시작)는 주민등록번호 규칙에 맞지 않으므로 가리지 않는다(다른 번호를 함부로 지우지 않는다).
@pytest.mark.parametrize("written", ["900101-0234567", "900101-9234567"])
def test_mask_leaves_thirteen_digits_that_are_not_an_rrn(written):
    assert mask_pii(f"번호 {written} 끝") == f"번호 {written} 끝"


# 한글에 바로 붙여 쓴 번호("번호는010-…")도 가린다. 한글 글자는 숫자가 아니라서 번호의 시작으로 본다.
def test_mask_number_glued_to_korean():
    assert mask_pii("제번호는010-1234-5678입니다") == "제번호는[전화번호]입니다"
    assert mask_pii("메일은a.b@c.com입니다") == "메일은[이메일]입니다"


# 더 긴 숫자열의 일부를 번호로 오인해 잘라 내지 않는다(예: 14자리 숫자, 5개의 숫자 묶음).
def test_mask_does_not_cut_inside_longer_numbers():
    s = "접수번호 20250101234567 와 0101234567890123"
    assert mask_pii(s) == s


# 문자열 안의 번호를 여러 개 한꺼번에 가린다.
def test_mask_several_in_one_text():
    assert (
        mask_pii("010-1111-2222 또는 a@b.kr, 900101-2345678")
        == "[전화번호] 또는 [이메일], [주민번호]"
    )


# 이미 가려진 글을 다시 가려도 같다(마스킹을 두 군데에서 거쳐도 안전하다).
def test_mask_is_idempotent():
    once = mask_pii("010-1234-5678 / a@b.com / 900101-1234567")
    assert mask_pii(once) == once


# dict·list 안쪽의 문자열까지 가리고, 키와 숫자·불리언·None은 그대로 둔다. 원본 객체는 바꾸지 않는다.
def test_mask_value_walks_nested_structures():
    original = {"q": "010-1234-5678", "n": 3, "ok": True, "none": None, "list": ["a@b.com", {"x": "900101-1234567"}]}
    snapshot = json.dumps(original, ensure_ascii=False)
    assert mask_value(original) == {
        "q": "[전화번호]",
        "n": 3,
        "ok": True,
        "none": None,
        "list": ["[이메일]", {"x": "[주민번호]"}],
    }
    assert json.dumps(original, ensure_ascii=False) == snapshot


# JSON 글(Langfuse가 span 속성으로 저장하는 모양)은 풀어서 가린다.
# \uXXXX로 이스케이프된 한글 끝자리 숫자("는" 의 4)가 번호 바로 앞에 붙어서, 원문 글에 정규식을 쓰면 번호를 놓친다.
def test_mask_serialized_decodes_json_before_masking():
    raw = '{"q": "\\uc81c \\ubc88\\ud638\\ub294010-1234-5678 \\uc785\\ub2c8\\ub2e4"}'
    assert "010-1234-5678" in raw  # 시험 자료가 의도한 모양인지 확인
    assert mask_pii(raw) == raw  # 정규식만 쓰면 놓친다는 증거
    masked = mask_serialized(raw)
    assert "010-1234-5678" not in masked
    assert json.loads(masked) == {"q": "제 번호는[전화번호] 입니다"}


# 바뀔 것이 없으면 입력 글자를 그대로 돌려준다(필요 없는 다시 쓰기·인코딩 변경이 없다).
def test_mask_serialized_returns_same_text_when_nothing_to_mask():
    raw = '{"q": "\\uc81c \\ubc88\\ud638"}'
    assert mask_serialized(raw) is raw


# JSON이 아닌 글(예: 그냥 문장)은 문장으로 보고 가린다.
def test_mask_serialized_falls_back_to_plain_text():
    assert mask_serialized("전화는 010-1234-5678") == "전화는 [전화번호]"
    assert mask_serialized("") == ""
