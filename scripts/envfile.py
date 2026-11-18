"""스크립트들이 키를 읽는 공통 방법: `.env` 파일 + 환경 변수.

역할: 앱 설정(`app/config.py`)은 `.env`를 읽지만, 앱 설정은 필수 값이 모두 있어야 만들어진다.
수집·측정 스크립트는 키 한두 개만 필요하므로 이 작은 함수로 필요한 값만 읽는다.
사용처: scripts/step0_probe.py(Gemini·Cohere 키), scripts/collect.py(법제처 OC 값).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values


def read_env(dotenv_path: Path | str = ".env") -> dict[str, str]:
    """`.env` 파일과 환경 변수를 합쳐 돌려준다. 같은 이름이면 환경 변수가 이긴다.

    왜 필요한가: `.env`에 넣은 키가 스크립트에 보이지 않던 문제를 없앤다. 앱 설정과 같은 우선순위
    (환경 변수 > .env)로 맞춰, 두 곳 어디에 넣어도 동작하게 한다.
    파일이 없으면 환경 변수만 돌려준다. 키 값은 이 함수 안에서도 출력하지 않는다.
    """
    from_file = {k: v for k, v in dotenv_values(dotenv_path).items() if v is not None}
    return {**from_file, **os.environ}
