"""서비스 설정(Settings).

역할: 모델 이름, 호출 한도, 서버 주소 같은 값을 한곳에 모은다. 값은 환경 변수(접두어 `LABOR_`)나
서버의 `.env` 파일에서 읽고, 없으면 아래 기본값을 쓴다(기본값은 계획서 Global Constraints와 같다).

비밀번호는 원문이 아니라 bcrypt **해시**만 받는다(`admin_password_hash`, `test_password_hash`).
기본값이 없는 항목(키·해시 등)은 필수이며, 빠지면 서버가 시작될 때 바로 오류가 난다 — 조용히 틀린 채 도는 것보다 낫다.

사용처: 거의 모든 모듈. 테스트에서는 `tests/conftest.py`의 `settings_factory`로 가짜 값을 채워 만든다.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경 변수/.env에서 읽는 설정. 환경 변수 이름은 `LABOR_` + 필드 이름(대문자)이다."""

    model_config = SettingsConfigDict(
        env_prefix="LABOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env에 이 클래스가 모르는 줄이 있어도 오류로 멈추지 않는다.
    )

    # --- 모델 ---
    embedding_model: str = "gemini-embedding-2"
    embedding_dim: int = 768  # 낮은 차원으로 OpenSearch 메모리를 아낀다(스펙 6.2).
    llm_model: str = "gemini-3.5-flash-lite"  # 임시값. Task 17의 모델 비교 뒤에 정한다.

    # --- 호출·사용 한도 ---
    test_ip_limit: int = 5  # `test` 계정이 같은 IP에서 쓸 수 있는 누적 횟수
    rerank_monthly_cap: int = 950  # Cohere 시험 키 월 1,000회에서 여유를 둔 값. 넘으면 리랭크를 끈다.
    max_llm_calls: int = 8  # 질문 하나가 쓸 수 있는 LLM 호출 총량(무료 한도 보호)
    max_regen: int = 2  # 답변 재생성 최대 횟수
    max_research: int = 2  # 재검색 최대 횟수
    max_question_chars: int = 1000  # 이보다 긴 질문은 거절한다.

    # --- 검색 ---
    search_depth: int = 150  # 하위 쿼리마다 가져올 후보 수. Task 9에서 100·150·200을 비교해 정한다.
    # 충분성 판단 임계값: 리랭크 1위 점수가 이보다 낮으면 "근거 부족". Task 9 측정(docs/step0-report.md 8절)의 일상어형
    # 후보를 사용자가 정했다(2026-11-18). 표본이 작아서(정답 없는 질문 6건) 서비스 전체를 돌리는 Task 17에서 다시 잰다.
    rerank_min_score: float = 0.15

    # --- 서버·저장소 ---
    # 외부에 열지 않고 같은 서버 안에서만 접근한다. 보안 기능을 켠 채로 쓰므로(사용자 결정 2026-11-18) https + 계정이다.
    opensearch_url: str = "https://127.0.0.1:9200"
    opensearch_user: str = "admin"
    index_name: str = "labor-rag-v1"
    sqlite_path: str = "data/app.db"
    # 대화 체크포인트(LangGraph가 질문마다 쌓는 상태)는 앱 DB와 **다른 파일**에 둔다. 계속 커지는 파일이라 정리 스크립트
    # (scripts/prune_checkpoints.py, 기본 대상이 이 경로)가 그 파일만 다루게 하려는 것이다.
    checkpoint_path: str = "data/checkpoints.db"
    # 이 주소에서 온 요청의 X-Forwarded-For만 믿는다(위조된 헤더로 IP 제한을 우회하는 것을 막는다).
    trusted_proxies: list[str] = Field(default_factory=lambda: ["127.0.0.1"])

    # --- 필수 값(기본값 없음): 서버 .env에서만 채운다 ---
    law_api_oc: str
    gemini_api_key: str
    cohere_api_key: str
    opensearch_password: str  # 설치할 때 사용자가 서버에서 직접 정한 admin 비밀번호. 서버 .env에만 둔다.
    admin_password_hash: str
    test_password_hash: str
    session_secret: str
    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_host: str
