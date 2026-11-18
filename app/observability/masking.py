"""개인정보 마스킹: 주민등록번호·휴대전화·이메일을 `[주민번호]`·`[전화번호]`·`[이메일]`로 바꾼다.

역할: Langfuse Cloud에 보내는 **기록**에서 개인정보를 가린다. 사용자 질문은 Langfuse에도 저장되는데(스펙 6.7), 서버 쪽
마스킹은 유료라서 무료판에서는 우리 코드(SDK 쪽)가 보내기 전에 가려야 한다.
사용처: observability/langfuse_setup.py(span 속성 마스킹), graph/build.py(수동 기록 이벤트).

핵심 개념 — **기록에만 적용하고 LLM 입력에는 적용하지 않는다**: LLM에는 질문 원문이 그대로 가야 질문의 뜻이 보존된다.
(번호를 가려도 질문의 뜻은 거의 달라지지 않지만, 마스킹이 틀리면 답변 품질이 조용히 나빠지므로 기록 쪽만 가린다.)

한계(스펙이 약속한 범위): 이름·주소·회사명 같은 **패턴이 없는** 개인정보는 가리지 못한다. 화면의 "개인정보를 입력하지 마세요"
안내가 이 부분을 맡는다. 유선 전화번호도 가리지 않는다(휴대전화만).
"""

import json
import re
from typing import Any

# 숫자는 앞뒤가 숫자가 아닐 때만 번호로 본다. 더 긴 숫자열(접수번호 등)의 한 토막을 잘라 내지 않기 위해서다.
# 한글 글자는 숫자가 아니므로 "번호는010-…"처럼 붙여 쓴 경우도 번호의 시작으로 잡힌다.

# 주민등록번호: 앞 6자리(생년월일) + 뒷자리 7자리(첫 자리 1~8). 하이픈이나 공백이 없어도 된다.
_RRN = re.compile(r"(?<!\d)\d{6}[- ]?[1-8]\d{6}(?!\d)")

# 휴대전화: 010·011·016·017·018·019 + 3~4자리 + 4자리. 구분자는 하이픈·공백·점 중 하나이거나 없다. +82 국제 표기도 받는다.
_PHONE = re.compile(r"(?<!\d)(?:\+82[- ]?0?|0)1[016789][- .]?\d{3,4}[- .]?\d{4}(?!\d)")

# 이메일: 앞부분은 ASCII만 허용한다. 한글까지 허용하면 "메일은a@b.com"에서 "메일은a"까지 이메일로 삼켜 버린다.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def mask_pii(text: str) -> str:
    """글 속의 주민등록번호·휴대전화·이메일을 가린다. 나머지(법령 문장, 금액, 시간 숫자)는 그대로 둔다.

    적용 순서는 결과에 영향이 없다: 패턴마다 앞뒤가 숫자가 아니어야 하므로, 13자리 주민등록번호의 일부를 전화번호 패턴이 잘라 내는 일은
    없다(순서를 바꿔 무작위 입력 30만 건을 비교했을 때 차이가 없었다). 주민등록번호를 먼저 두는 것은 읽는 순서일 뿐이다.
    이미 가려진 글에 다시 적용해도 결과가 같다(`[전화번호]`는 어느 패턴에도 걸리지 않는다).
    """
    text = _RRN.sub("[주민번호]", text)
    text = _PHONE.sub("[전화번호]", text)
    return _EMAIL.sub("[이메일]", text)


def mask_value(value: Any) -> Any:
    """dict·list 안쪽까지 들어가서 모든 문자열을 `mask_pii`로 가린 **새** 값을 돌려준다. 원본은 바꾸지 않는다.

    키(dict의 열쇠)와 숫자·불리언·None은 그대로 둔다. tuple은 list로 바뀐다(어차피 JSON으로 보낼 값이다).
    """
    if isinstance(value, str):
        return mask_pii(value)
    if isinstance(value, dict):
        return {key: mask_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_value(item) for item in value]
    return value


def mask_serialized(text: str) -> str:
    """JSON으로 직렬화된 글을 가린다. Langfuse SDK가 span 속성(입력·출력·메타데이터)을 이 모양으로 저장한다.

    왜 JSON을 풀어서 가리나: SDK가 한글을 `\\uXXXX`로 이스케이프해서 저장한다(실제 확인). "번호는010-1234-5678"이
    `\\ub294010-1234-5678`이 되는데, `\\ub294`의 끝자리 `4`가 번호 바로 앞의 **숫자**가 되어 "앞이 숫자이면 번호가 아니다"
    규칙에 걸려 번호를 놓친다. JSON을 풀면 `는` 글자가 되어 정상으로 잡힌다.
    바꿀 것이 없으면 **입력 글자를 그대로**(같은 객체로) 돌려준다. JSON이 아닌 글은 일반 문장으로 보고 가린다.
    """
    if not text:
        return text
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return mask_pii(text)
    if not isinstance(data, (dict, list, str)):
        return text  # 숫자·불리언·null은 가릴 것이 없다
    masked = mask_value(data)
    return text if masked == data else json.dumps(masked, ensure_ascii=False)
