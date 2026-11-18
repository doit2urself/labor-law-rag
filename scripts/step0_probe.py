"""0단계 측정 도구: 무료 요금제의 호출 한도를 실제로 재 본다.

왜 필요한가: Gemini·Cohere 무료 한도의 분당·일일 숫자는 문서로 확인하지 못했다(스펙 9절 UNVERIFIED).
색인 예산(Task 7), 충분성 임계값(Task 11), 호출 상한(Task 13), 모델 선택(Task 17)이 모두 이 숫자에 기대므로
구현 전에 직접 재서 `docs/step0-report.md`에 남긴다.

사용법(프로젝트 루트에서, 키는 환경 변수 또는 git에 올라가지 않는 `.env`에만 넣는다 — 대화·커밋에 적지 않는다):
    python scripts/step0_probe.py gemini-chat   --model <모델> --max-calls 12
    python scripts/step0_probe.py gemini-embed  --model gemini-embedding-2 --max-calls 12
    python scripts/step0_probe.py cohere-rerank --model <모델> --max-calls 12
필요한 키 이름: Gemini는 LABOR_GEMINI_API_KEY, Cohere는 LABOR_COHERE_API_KEY (환경 변수가 `.env`보다 우선).

설계 메모: 이 도구는 Task 7·8·10의 어댑터를 쓰지 않고 각 SDK의 최소 호출만 쓴다. 어댑터는 재시도·오류 변환이
들어 있어서 "한도가 어디서 막히는지"를 가릴 수 있기 때문이다. 한도 초과(429)만 QuotaExceeded로 바꿔 멈춘다.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# `python scripts/step0_probe.py`로 직접 실행하면 sys.path[0]이 scripts/ 폴더라서 `import app`이 안 된다.
# 프로젝트 루트를 import 경로에 넣어 어디서 실행해도 되게 한다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.domain.errors import QuotaExceeded  # noqa: E402
from scripts.envfile import read_env  # noqa: E402  (테스트가 `scripts.step0_probe.read_env`로도 가져온다)


@dataclass(frozen=True)
class ProbeResult:
    """측정 결과 한 건.

    calls_ok: 한도에 걸리기 전까지 성공한 호출 수.
    first_429_at: 처음 한도 초과가 난 호출의 번호(1부터). 끝까지 안 걸렸으면 None.
    elapsed_s: 전체 소요 시간(초). retry_after_s: 서비스가 알려 준 대기 시간(초), 없으면 None.
    """

    calls_ok: int
    first_429_at: int | None
    elapsed_s: float
    retry_after_s: float | None


def probe_limit(
    call: Callable[[], None],
    max_calls: int,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> ProbeResult:
    """`call`을 최대 `max_calls`번 부르되, QuotaExceeded가 나면 거기서 멈춘다.

    한도 초과가 아닌 오류(키 오류, 모델 없음 등)는 그대로 올려서 사람이 원인을 보게 한다.
    `clock`을 바꿔 끼울 수 있게 한 이유: 테스트에서 진짜 시간을 기다리지 않기 위해서다.
    """
    start = clock()
    calls_ok = 0
    first_429_at: int | None = None
    retry_after_s: float | None = None
    for attempt in range(1, max_calls + 1):
        try:
            call()
        except QuotaExceeded as exc:
            first_429_at = attempt
            retry_after_s = exc.retry_after_s
            break
        calls_ok += 1
    return ProbeResult(calls_ok, first_429_at, clock() - start, retry_after_s)


def _parse_retry_delay(body: Any) -> float | None:
    """구글 API 오류 본문에서 `retryDelay`("34s" 형태)를 초 단위 숫자로 꺼낸다. 없거나 모양이 다르면 None.

    본문 모양(`error.details[].retryDelay`)은 구글 API 규약(google.rpc.RetryInfo)을 따른 가정이다.
    실제 429 응답에서 확인되기 전까지는 "있으면 쓰고, 없으면 None"으로만 다룬다.
    """
    try:
        for item in body["error"]["details"]:
            delay = item.get("retryDelay")
            if isinstance(delay, str) and delay.endswith("s"):
                return float(delay[:-1])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return None


def _gemini_error_to_quota(exc: Exception) -> QuotaExceeded | None:
    """Gemini SDK 예외가 한도 초과(HTTP 429)면 QuotaExceeded로 바꿔 돌려주고, 아니면 None."""
    from google.genai import errors as genai_errors

    if isinstance(exc, genai_errors.ClientError) and exc.code == 429:
        return QuotaExceeded(str(exc), retry_after_s=_parse_retry_delay(exc.details))
    return None


def build_gemini_chat_call(client: Any, model: str) -> Callable[[], None]:
    """Gemini 대화 모델을 아주 짧게 한 번 부르는 함수를 만든다. 출력을 16토큰으로 제한해 한도를 아낀다."""
    from google.genai import types

    def call() -> None:
        try:
            client.models.generate_content(
                model=model,
                contents="안녕",
                config=types.GenerateContentConfig(max_output_tokens=16),
            )
        except Exception as exc:
            quota = _gemini_error_to_quota(exc)
            if quota is not None:
                raise quota from exc
            raise

    return call


def build_gemini_embed_call(client: Any, model: str) -> Callable[[], None]:
    """Gemini 임베딩 모델을 한 번 부르는 함수를 만든다. 차원은 서비스와 같은 768로 요청한다."""
    from google.genai import types

    def call() -> None:
        try:
            client.models.embed_content(
                model=model,
                contents="근로기준법 제55조 유급휴일",
                config=types.EmbedContentConfig(output_dimensionality=768),
            )
        except Exception as exc:
            quota = _gemini_error_to_quota(exc)
            if quota is not None:
                raise quota from exc
            raise

    return call


def build_cohere_rerank_call(client: Any, model: str) -> Callable[[], None]:
    """Cohere 리랭크를 문서 3개로 한 번 부르는 함수를 만든다. 분당 호출 수(시험 키는 10회)를 재는 용도다."""
    import cohere

    def call() -> None:
        try:
            client.rerank(
                model=model,
                query="주휴수당 지급 요건",
                documents=[
                    "주휴수당은 1주 소정근로일을 개근한 근로자에게 주는 유급휴일 수당이다.",
                    "최저임금은 매년 고시되며 모든 사업장에 적용된다.",
                    "퇴직금은 계속근로기간 1년에 대하여 30일분 이상의 평균임금이다.",
                ],
                top_n=2,
            )
        except cohere.TooManyRequestsError as exc:
            # 대기 시간 헤더 이름은 확인하지 못했으므로 값은 비워 둔다(측정에서 응답을 보고 정한다).
            raise QuotaExceeded(str(exc)) from exc

    return call


# 종류별로 필요한 환경 변수 이름.
KEY_ENV: dict[str, str] = {
    "gemini-chat": "LABOR_GEMINI_API_KEY",
    "gemini-embed": "LABOR_GEMINI_API_KEY",
    "cohere-rerank": "LABOR_COHERE_API_KEY",
}

Factory = Callable[[str, str], Callable[[], None]]


def _real_gemini_client(api_key: str) -> Any:
    from google import genai

    return genai.Client(api_key=api_key)


def _make_gemini_chat(api_key: str, model: str) -> Callable[[], None]:
    return build_gemini_chat_call(_real_gemini_client(api_key), model)


def _make_gemini_embed(api_key: str, model: str) -> Callable[[], None]:
    return build_gemini_embed_call(_real_gemini_client(api_key), model)


def _make_cohere_rerank(api_key: str, model: str) -> Callable[[], None]:
    import cohere

    return build_cohere_rerank_call(cohere.ClientV2(api_key=api_key), model)


# 진짜 SDK 클라이언트를 만드는 기본 공장. 테스트에서는 가짜 공장으로 바꿔 끼운다.
DEFAULT_FACTORIES: dict[str, Factory] = {
    "gemini-chat": _make_gemini_chat,
    "gemini-embed": _make_gemini_embed,
    "cohere-rerank": _make_cohere_rerank,
}


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    factories: Mapping[str, Factory] | None = None,
) -> int:
    """명령줄 진입점. 종료 코드: 0 = 측정 끝, 1 = 한도 초과가 아닌 오류, 2 = 키 없음.

    결과는 `key=value` 한 줄로 출력해서 `docs/step0-report.md`에 그대로 옮기기 쉽게 한다.
    API 키 값은 어떤 출력에도 넣지 않는다.
    """
    parser = argparse.ArgumentParser(description="무료 요금제 호출 한도를 실제로 재 본다.")
    parser.add_argument("kind", choices=sorted(KEY_ENV))
    parser.add_argument("--model", required=True, help="측정할 모델 이름")
    parser.add_argument("--max-calls", type=int, default=12, help="최대 호출 수(무료 한도를 아끼려고 작게 둔다)")
    args = parser.parse_args(argv)

    # 테스트가 env를 직접 넘기면 .env를 읽지 않는다(진짜 키 파일이 테스트에 섞이지 않게 하려는 것).
    env = read_env() if env is None else env
    factories = DEFAULT_FACTORIES if factories is None else factories

    key_name = KEY_ENV[args.kind]
    api_key = env.get(key_name)
    if not api_key:
        print(f"환경 변수 {key_name}가 비어 있습니다. 이 PC의 환경 변수로 키를 넣고 다시 실행하세요.", file=sys.stderr)
        return 2

    ok_before_error = 0
    try:
        call = factories[args.kind](api_key, args.model)

        def counting_call() -> None:
            nonlocal ok_before_error
            call()
            ok_before_error += 1

        result = probe_limit(counting_call, args.max_calls)
    except Exception as exc:
        # 한도 초과가 아닌 오류: 종류와 메시지를 보여 준다(키 값이 섞여 있을 경우를 대비해 가린다).
        message = str(exc).replace(api_key, "***")[:300]
        print(
            f"kind={args.kind} model={args.model} calls_ok_before_error={ok_before_error} "
            f"error={type(exc).__name__}: {message}"
        )
        return 1

    print(
        f"kind={args.kind} model={args.model} calls_ok={result.calls_ok} "
        f"first_429_at={result.first_429_at} elapsed_s={result.elapsed_s:.2f} "
        f"retry_after_s={result.retry_after_s}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
