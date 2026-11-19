# 웹서버·HTTPS(IP 인증서) 기록 (Task 18)

서버 주소·계정은 적지 않았다(프로젝트 규칙 3). 서버 주소는 `<server-ip>`로 가렸다.

## 1. 지금 상태

**서버에서 실행했다(2026-11-19).** 이 문서의 1~3절은 서버에 손대기 전에 공식 자료를 읽고 정한 계획이고, 4절은 실제로 실행한 명령과 결과다.
Step 5(앱 배포), Step 6(Caddy + IP 인증서), Step 7(끝에서 끝까지 확인)은 끝났다. 5절에서 "아직 모름"인 항목(자동 갱신, SSE 실시간 도착, 피드백 점수)은 확인하지 못한 것이다. 갱신이 실제로 되는지는 2026-11-22 이후에야 알 수 있다.

## 2. 웹서버 선택: Caddy를 먼저 시험하고, 안 되면 Nginx + ACME 클라이언트

계획서 Step 6의 순서를 따른다. 근거(2026-11-19에 읽은 것):

| 사실 | 출처 |
|---|---|
| Let's Encrypt의 IP 주소 인증서는 **2026-01-15에 정식 제공**됐다. 유효기간은 **160시간**(6일 남짓), ACME 클라이언트가 **`shortlived` 프로파일**을 골라야 받는다. IP 인증서는 shortlived만 가능하다 | [Let's Encrypt, 6-day and IP general availability](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability) |
| IP 주소는 DNS 방식으로 증명할 수 없고 `http-01`·`tls-alpn-01`만 된다(그래서 **80번 포트가 열려 있어야 한다**) | [Let's Encrypt, first IP address certificate](https://letsencrypt.org/2025/07/01/issuing-our-first-ip-address-certificate) |
| Caddy 이슈 #7399: `profile shortlived` + IP 주소 사이트에서 "IP 인증서를 받을 수 없다"는 오류가 났고, 메인테이너가 **새 빌드에서는 Let's Encrypt IP 인증서를 허용하는 코드가 있다**고 답했다. 업그레이드 뒤 **IPv4는 발급에 성공**했고, IPv6 문제는 2026-04-25에 master에서 해결되어 이슈가 닫혔다. **해결된 정확한 릴리스 번호는 이슈에 없다** | [caddyserver/caddy#7399](https://github.com/caddyserver/caddy/issues/7399) |
| Caddy 설정 문법(`issuer acme { profile shortlived }`)은 이슈 #7399의 설정과 커뮤니티 글에 나온다. 공식 문서에서 IP 인증서를 설명한 부분은 찾지 못했다 | [Caddy 커뮤니티 글](https://caddy.community/t/shortlived-certificates-from-lets-encrypt/33399) |

그래서 판단은 이렇다.

- 이 서버의 주소가 IPv4인지 IPv6인지는 **아직 확인하지 않았다**(서버에서 `ip -br addr`로 읽기 전용 확인). 이슈에서 업그레이드 뒤 성공한 것은 IPv4이고, IPv6는 2026-04-25에 master에서 고쳐졌다고만 적혀 있다(릴리스 번호 없음). IPv4라면 이슈와 같은 경우다.
- Ubuntu 24.04의 기본 저장소 Caddy는 오래된 버전일 가능성이 크다(추정, `apt-cache policy caddy`로 서버에서 확인한다). 오래된 빌드는 #7399의 첫 오류를 낸다. 그래서 **서버에 설치된 Caddy의 버전을 먼저 확인**하고, 낮으면 Caddy 공식 배포(저장소 또는 GitHub 릴리스, 체크섬 확인)로 최신을 받는다.
- 그래도 IP 인증서를 못 받으면 계획서 (b)대로 Nginx + ACME 클라이언트(shortlived 프로파일을 지원하는 것)로 간다. 그 경우 이 문서에 이유와 명령을 적는다.

## 3. 설정 파일

| 파일 | 서버에서의 위치 | 비고 |
|---|---|---|
| `deploy/labor-rag.service` | `/etc/systemd/system/labor-rag.service` | uvicorn `--workers 1 --no-proxy-headers`, 사용자 `labor`, 작업 폴더 `/opt/labor-rag` |
| `deploy/Caddyfile` | `/etc/caddy/Caddyfile` | 서버 IP는 `{$LABOR_SERVER_IP}`로 받는다(파일에 주소를 적지 않기 위해). 값은 서버의 caddy 서비스 드롭인에서만 정한다 |

설계 결정과 이유:

- **화면 파일은 `/var/www/labor-rag`에 따로 둔다**(2026-11-19 수정). 처음 계획은 Caddy가 `/opt/labor-rag/frontend/dist`를 바로 읽는 것이었는데, 그 폴더는 `labor`만 들어갈 수 있고(750, 소유 `labor:labor`) Caddy 계정은 `caddy`(그룹 `caddy`,`www-data`)라 읽지 못한다(추론: 권한 값에서 나온 결론, 실제 오류는 보지 않았다). 폴더를 열어 주면 `data/checkpoints.db`(대화 기록)까지 보여서, 공개해도 되는 화면 파일만 복사했다.
- **웹서버가 화면 파일을 직접 준다.** 화면은 `/api/...` 상대 경로만 부른다(`frontend/src/api.ts`). 그래서 Caddy가 화면 파일을 주고 `/api/*`만 8000번으로 넘기면 된다. FastAPI에 정적 파일 서빙을 넣지 않았다(`app/api/main.py`는 그대로). 서버에는 node가 필요 없다 — 화면은 이 PC에서 `npm run build`한 결과만 옮긴다.
- **요청 본문 32KB 제한을 웹서버에서 건다.** 앱은 질문 글자 수(1,000자)만 제한하고 본문 크기는 제한하지 않는다(원장 Task 14 판정). 한글 1,000자 질문 + JSON 포장이 약 3.1KB(계산: 1,000자 × 3바이트 + 포장)라 32KB면 넉넉하다.
- **HSTS 헤더는 넣지 않았다.** 인증서가 6일마다 갱신되는데 갱신이 어긋나면 HSTS 때문에 브라우저가 접속 자체를 막는다. 필요하면 갱신이 안정적으로 도는 것을 확인한 뒤 넣는다.
- **`--no-proxy-headers`:** uvicorn이 `X-Forwarded-For`로 접속 주소를 바꾸는 기능을 끈다. IP를 읽는 규칙은 앱의 `client_ip`(접속 주소가 신뢰하는 프록시일 때만 헤더를 읽고 오른쪽부터 걷는다) 한 곳에서만 맡는다. 실제로 그렇게 도는지는 Step 7에서 확인한다(5절).
- **systemd 유닛이 `.env`를 직접 읽지 않는다.** 작업 폴더의 `.env`를 앱이 읽는다. 비밀번호 해시의 `$`를 systemd `EnvironmentFile`이 변형 없이 읽는지 확인하지 못했고, 앱이 직접 읽는 방식은 시험으로 확인했기 때문이다.

## 3-1. 올릴 파일을 만들 때 겪은 문제: `git archive`가 CRLF로 내보낸다

이 PC의 git은 `core.autocrlf=true`다. 그냥 `git archive HEAD`로 묶었더니 **커밋된 파일(LF)이 CRLF로 바뀌어 나왔다**(2026-11-19 측정: `deploy/labor-rag.service`에 CR 51개, `deploy/Caddyfile`에 48개). systemd 유닛과 Caddyfile은 줄 끝의 CR 때문에 값이 어긋날 수 있다.
해결: `git -c core.autocrlf=false -c core.eol=lf archive --format=tar -o <파일> HEAD`. 이렇게 만든 묶음은 파일 166개 모두 CR 0개임을 파이썬 `tarfile`로 확인했다. 올리는 묶음은 항상 이 명령으로 만든다. (GNU tar는 경로의 `C:`를 원격 호스트로 오해해서 검사에 쓸 수 없었다 — 검사는 파이썬으로 했다.)

## 4. 서버에서 실행한 명령과 결과

서버 작업은 명령마다 사용자 승인을 받았다. 서버에는 `root`로 접속한다. 이 절은 2026-11-19에 실제로 실행한 것만 적는다.

### 4-1. 앱 배포 (Step 5)

| # | 구분 | 한 일 | 결과 |
|---|---|---|---|
| 1 | 읽기 | OS·파이썬·Caddy·포트·메모리 점검 4묶음 | Ubuntu 24.04.5, Python 3.12.3, `labor` 계정과 `/opt/labor-rag` 없음, Caddy 없음(apt 후보 2.6.2), 웹서버 모두 꺼짐, ufw 꺼짐, 80·443·8000 듣는 것 없음(빈 grep 결과로 본 추정), 전역 IPv4 1개·IPv6 1개, 사용 가능 메모리 5.5GiB, 디스크 92GB 여유, OpenSearch 켜져 있음 |
| 2 | 변경 | `useradd --system --home-dir /opt/labor-rag --shell /usr/sbin/nologin labor`, `mkdir -p /opt/labor-rag/data`, `chown -R labor:labor`, `chmod 750 /opt/labor-rag` | uid 997. 되돌리기: `userdel labor; rm -rf /opt/labor-rag` |
| 3 | 변경 | 소스(`git archive`, LF) 1.38MB와 화면(`frontend/dist`) 0.2MB를 `scp`로 `/tmp`에 올리고 `tar -xf … -C /opt/labor-rag`, `chown -R labor:labor`, `/tmp`의 묶음 삭제 | 서버에 도착한 `deploy/labor-rag.service`·`deploy/Caddyfile`의 CR 바이트 0개 |
| 4 | 변경 | `python3 -m venv`가 **실패**: `ensurepip is not available` → 서버에 `python3.12-venv`가 없었다 | 사용자 승인 후 `apt-get update`, 모의 실행(`apt-get install -s`: 새 패키지 3개·업그레이드 0·삭제 0), `apt-get install -y python3.12-venv`. 설치 중 "needrestart is being skipped since dpkg has failed"가 출력됐지만 `dpkg --audit`는 비어 있었고 `python3.12-venv`·`opensearch` 모두 `install ok installed`였다. 되돌리기: `apt-get remove python3.12-venv` |
| 5 | 변경 | `rm -rf /opt/labor-rag/.venv`(실패한 조각), `python3 -m venv /opt/labor-rag/.venv`(labor 계정), `pip install -r requirements.txt` | 종료 코드 0 |
| 6 | 읽기 | 서버에서 `pytest -q`(labor 계정, 파이썬 3.12, 리눅스) | **604 passed, 4 skipped, 8 deselected.** 건너뛴 4개는 모두 `data/manual`(법령 원본 파일, 저장소에 없음)이 없어서다. 이 PC의 608 passed와 합이 같다(604+4=608) |
| 7 | 변경 | PC의 `.env`를 `scp`로 `/opt/labor-rag/.env`에 복사, `chown labor:labor`, `chmod 600` | 27줄, CR 0개. 내용은 읽지 않았다 |
| 8 | 읽기 | labor 계정으로 `Settings()` 읽기 점검(형식만 출력) | 해시 2개 bcrypt 형식·60자, 세션 비밀 64자, `law_api_oc` 비어 있어도 통과 |
| 9 | 변경 | `cp deploy/labor-rag.service /etc/systemd/system/`, `systemctl daemon-reload`, `systemctl enable --now labor-rag` | `active`, `enabled`. 로그에 오류 없음. `ProtectSystem=strict`·`ReadWritePaths=data`·`NoNewPrivileges` 샌드박스 설정이 **첫 시작에서 그대로 통과**했다. 되돌리기: `systemctl disable --now labor-rag; rm /etc/systemd/system/labor-rag.service; systemctl daemon-reload` |
| 10 | 읽기 | `curl http://127.0.0.1:8000/api/health` | `{"status":"ok","opensearch":"green","index_docs":732}` (HTTP 200). 계획서 Step 5의 기대 결과와 같다. `/api/me`(로그인 없이)는 401. 8000번은 `127.0.0.1`에서만 듣는다. `data/app.db`·`checkpoints.db`가 만들어졌다 |

배운 점: PowerShell 5.1에서 ssh로 넘기는 원격 명령에는 **큰따옴표를 쓰면 지워진다**(두 번 겪었다). 원격 명령은 작은따옴표 문자열 안에 큰따옴표 없이 쓰고, 따옴표가 필요하면 `''`로 쓴다.

### 4-2. 웹서버와 IP 인증서 (Step 6) — 2026-11-19, 명령마다 사용자 승인

| # | 구분 | 한 일 | 결과 |
|---|---|---|---|
| 11 | 변경 | Caddy 공식 apt 저장소 등록(`dl.cloudsmith.io`의 서명 키·목록), `apt-get update`, 모의 실행(`apt-get install -s caddy`: 새 패키지 1개·업그레이드 0·삭제 0), `apt-get install -y caddy` | **Caddy v2.11.9**. `caddy` 계정(uid 995)과 `caddy.service`가 생기고 설치 직후 기본 화면으로 켜졌다. 서명 키는 처음 받은 것을 그대로 믿었다(별도 지문 대조 없음). 되돌리기: `apt-get remove --purge caddy; rm /etc/apt/sources.list.d/caddy-stable.list /usr/share/keyrings/caddy-stable-archive-keyring.gpg; apt-get update` |
| 12 | 변경 | 서버 IP를 `/etc/systemd/system/caddy.service.d/labor-ip.conf`(`Environment=LABOR_SERVER_IP=…`)에 둠. PC가 ssh 설정의 주소를 임시 파일로 만들어 `scp`했고, 주소는 어떤 출력에도 찍지 않았다 | ssh 설정의 주소가 IPv4 숫자 주소임을 확인, 서버가 그 주소를 실제 네트워크 카드에 갖고 있음(`match`), CR 0개. 되돌리기: `rm -r /etc/systemd/system/caddy.service.d; systemctl daemon-reload` |
| 13 | 읽기 | 기본 화면 상태에서 PC → 서버 80·443·9200 | 80: HTTP 200(**80번이 밖에서 닿는다**), 443: 연결 거부(아직 듣는 프로그램 없음), 9200: 연결 안 됨 |
| 14 | 변경 | `/var/www/labor-rag`를 만들어 화면 파일 4개만 복사(`chmod -R a+rX`), 고친 `Caddyfile`을 `scp`, 기본 설정을 `/etc/caddy/Caddyfile.dist`로 백업하고 `/etc/caddy/Caddyfile`로 설치, `caddy validate` | `Valid configuration`. 되돌리기: `cp /etc/caddy/Caddyfile.dist /etc/caddy/Caddyfile; systemctl restart caddy; rm -rf /var/www/labor-rag` |
| 15 | 변경 | `systemctl restart caddy` → Let's Encrypt에 IP 인증서 요청 | 로그: ACME 계정 새로 등록(이메일 없음) → `http-01` 챌린지 → 서버가 키 인증을 7번 응답(여러 곳에서 검증하는 것으로 보임, 추정) → `authorization finalized`(valid) → **`certificate obtained successfully`**(발급 시각 2026-11-19 01:44:33 UTC). 첫 시도에서 성공, 재시도 없음 |

인증서 측정값(PC에서 `openssl s_client`, 주소는 가림):

| 항목 | 값 |
|---|---|
| 발급자 | `C=US, O=Let's Encrypt, CN=YE1` |
| 시작 | 2026-11-19 00:45:58 GMT |
| 만료 | 2026-11-25 16:45:57 GMT |
| 유효기간 | **159.9997시간**(계산: 만료 − 시작) → 계획서 기대값(약 160시간)과 같다 |

밖(이 PC)에서 확인한 것:

| 호출 | 결과 |
|---|---|
| `https://<ip>/` (인증서 검증 켜짐, Windows 인증서 저장소) | HTTP 200, `ssl_verify_result=0`(검증 통과), HTTP/1.1 |
| `https://<ip>/api/health` | `{"status":"ok","opensearch":"green","index_docs":732}` |
| `https://<ip>/api/me`(로그인 없이) | 401 |
| `http://<ip>/` | 308 → `https://<ip>/` (Caddy의 자동 이동) |
| `https://<ip>:9200/` | HTTP 000, curl 종료 코드 7(연결 안 됨) — 밖에서 닫혀 있음. 이 PC 한 곳에서만 본 것 |

**자동 갱신은 아직 관측하지 못했다.** 확인된 것은 두 가지뿐이다: (1) 로그에 `started background certificate maintenance`(Caddy가 인증서를 백그라운드에서 관리), (2) 발급 직후 Let's Encrypt가 알려 준 **갱신 권장 시간대(ACME Renewal Information)**가 로그에 찍혔다 — 시작 2026-11-22 07:39:48 UTC(발급 +77.9시간) ~ 끝 2026-11-22 10:50:38 UTC(+81.1시간). 만료(+159.0시간)보다 약 3일 앞이다. 그래서 **2026-11-22 10:50 UTC(한국 시각 19:50) 이후** 같은 `openssl` 명령으로 `notBefore`가 바뀌었는지 보면 갱신이 실제로 되었는지 알 수 있다. 그 전에는 "갱신이 예약됐다"까지만 말할 수 있다.

### 4-3. 끝에서 끝까지 확인 (Step 7) — 2026-11-19

| 확인 | 방법 | 결과 |
|---|---|---|
| 로그인 화면과 고정 문구 | 이 PC의 Edge(헤드리스)로 `https://<ip>/` 스크린샷 | 인증서 오류 없이 열렸고, 로그인 폼과 「법률 자문이 아닙니다. 출처의 원문을 확인하세요」·「개인정보를 입력하지 마세요」가 보였다. 세 번째 「출처: 법제처 국가법령정보센터」는 설계상 답변 카드 아래에 붙어서 로그인 화면에는 없다. 서버가 내보내는 JS 파일에는 세 문구가 모두 들어 있다(문자열 검색) — **실제 답변 화면에 렌더링된 것은 아직 눈으로 보지 못했다** |
| `test` 5회 제한, 위조 헤더 무시, `admin` 무제한 | 사용자가 자기 터미널에서 `scripts/verify_deploy.py` 실행(비밀번호는 `getpass`로 입력, 출력에는 주소가 `<ip>`로 가려짐) | **합격.** `test`: 질문 1~5는 HTTP 200, 6·7번째는 429 `"이 IP의 사용 횟수(5회)를 초과했습니다."`(위조 `X-Forwarded-For`가 붙은 7번째도 429). `admin`: 위조 헤더 3번을 섞은 7번 모두 200 |
| 서버 DB에서의 직접 확인 | `ip_usage` 표의 행 수와 횟수만 읽음(주소는 출력하지 않음) | 행 **1개**, 횟수 **5**. 위조 헤더가 별도 주소로 세어졌다면 행이 늘었을 것이다 |
| 시험 기록 되돌리기(사용자 승인) | `DELETE FROM ip_usage`(labor 계정) | 1행 삭제, 표 비어 있음. 그 뒤 이 PC에서 `test`로 다시 5번 쓸 수 있다 |
| 위 시험이 통과한 의미 | — | Caddy가 위조된 `X-Forwarded-For`를 덮어쓰거나, 앱의 `client_ip`가 오른쪽부터 읽어 걸러 낸다(둘 중 무엇이 막았는지는 구분하지 않았다). 어느 쪽이든 서버 전체 경로에서 우회가 되지 않았다 |
| 시험의 한계 | — | 질문은 모두 범위 밖 질문이라 분석 단계에서 거절되었다(검색·리랭크·생성은 이 시험에서 쓰이지 않음). 실제 검색·답변 경로의 서버 동작은 Step 8의 평가 실행으로 본다. IP를 하나만 써서 시험했다(다른 IP에서의 동작은 코드 시험으로만 확인) |

## 5. 서버에서 확인할 것

| 항목 | 방법 | 결과 |
|---|---|---|
| 서버에 Caddy가 이미 있는가, 버전은 | `caddy version`, `apt-cache policy caddy` | **없었다**(apt 후보 2.6.2). 공식 저장소에서 v2.11.9 설치(4-2의 11) |
| 80·443 포트가 밖에서 닿는가 | PC에서 `curl` | **확인됨(2026-11-19).** 80 → 200, 443 → 200(인증서 검증 통과). 이 PC 한 곳에서만 본 것 |
| Caddy가 IP 인증서를 받는가 | `openssl s_client … \| openssl x509 -noout -issuer -enddate` | **확인됨.** Let's Encrypt `YE1`, v2.11.9, IPv4, 첫 시도 성공 |
| 인증서 유효기간이 약 160시간인가 | `notAfter` − `notBefore` | **확인됨.** 159.9997시간 |
| **자동 갱신이 실제로 도는가** | 갱신 권장 시간대(2026-11-22 07:39~10:50 UTC) 이후 `notBefore` 변화 확인 | **아직 모름.** 예약됐다는 로그만 있다. 2026-11-22 10:50 UTC 이후 다시 본다 |
| 위조 `X-Forwarded-For` 무시 | PC에서 `X-Forwarded-For` 헤더를 붙여 `test`로 질문, 횟수가 헤더 값이 아니라 실제 IP로 세어지는지 | **확인됨(2026-11-19).** 4-3절. 위조 헤더 3번을 섞어 보냈는데 6·7번째가 429였고, 서버 `ip_usage` 표는 행 1개·횟수 5였다 |
| SSE가 모이지 않고 바로 오는가 | 채팅 응답에서 진행 이벤트가 하나씩 도착하는지 | **아직 모름.** 점검 스크립트는 응답 전체 시간(1~2초)만 쟀고, 이벤트가 하나씩 도착하는지는 보지 않았다. `flush_interval -1`을 설정했을 뿐이다. 실제 긴 질문을 브라우저에서 볼 때 확인한다 |
| `$`가 든 해시가 서버 `.env`에서 변형 없이 읽히는가 | 앱이 시작되면 통과(시작할 때 해시 형식을 검사한다) | **확인됨(2026-11-19).** 앱이 읽은 해시가 bcrypt 형식·60자이고 서비스가 시작됐다(4-1의 8·9). 실제 로그인은 Step 7에서 본다 |
| 9200 포트가 밖에서 닫혀 있는가 | PC에서 `curl -m 5 https://<server-ip>:9200` 이 실패 | **확인됨.** HTTP 000, 종료 코드 7. 이 PC 한 곳에서만 본 것 |
| 피드백 점수 쓰기(Langfuse v3 제거 뒤) | 실제 👍 한 번 후 Langfuse에서 점수 확인 | 측정 전 |
