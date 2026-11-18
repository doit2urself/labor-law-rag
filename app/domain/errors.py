"""서비스 전체가 함께 쓰는 예외.

역할: 외부 서비스(Gemini·Cohere·법제처)가 실패했을 때, 어댑터가 각자의 라이브러리 예외를 이 예외로 바꿔서
던진다. 그러면 그래프와 API는 특정 라이브러리를 몰라도 "한도 초과인지 / 그 밖의 실패인지"만 구분하면 된다.

사용처: adapters(던지는 쪽), graph·api(잡는 쪽).
"""


class QuotaExceeded(Exception):
    """무료 호출 한도를 넘었다(HTTP 429 등). 사용자에게는 친절한 안내 문구를 보여 주고 사용 횟수는 차감하지 않는다."""


class ExternalServiceError(Exception):
    """외부 서비스가 한도 초과가 아닌 이유로 실패했다(시간 초과, 서버 오류 등)."""


class LawApiError(Exception):
    """법제처 국가법령정보 공동활용 API 호출이 실패했다."""


class LawApiAuthError(LawApiError):
    """법제처 API가 사용자 인증(OC 값, 등록한 서버 IP)을 거부했다.

    다른 법령 API 오류와 구분하는 이유: 승인 전이거나 IP가 다르면 재시도해도 소용이 없고,
    사람이 신청·등록 상태를 확인해야 하기 때문이다.
    """
