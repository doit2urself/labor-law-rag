# 웹서버·HTTPS(IP 인증서) 기록 (Task 18)

서버 주소·계정은 적지 않았다(프로젝트 규칙 3). 서버 주소는 `<server-ip>`로 가렸다.

## 1. 지금 상태

**아직 서버에서 실행하지 않았다(2026-11-19).** 이 문서의 1~3절은 서버에 손대기 전에 공식 자료를 읽고 정한 계획이다.
4절 이후(실행한 명령과 결과)는 서버 작업을 하면서 채운다. 채우지 않은 칸은 "측정 전"이라는 뜻이다.

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

- **웹서버가 화면 파일을 직접 준다.** 화면은 `/api/...` 상대 경로만 부른다(`frontend/src/api.ts`). 그래서 Caddy가 `frontend/dist`를 주고 `/api/*`만 8000번으로 넘기면 된다. FastAPI에 정적 파일 서빙을 넣지 않았다(`app/api/main.py`는 그대로). 서버에는 node가 필요 없다 — 화면은 이 PC에서 `npm run build`한 결과만 옮긴다.
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

## 5. 서버에서 확인할 것 (아직 모름)

| 항목 | 방법 | 결과 |
|---|---|---|
| 서버에 Caddy가 이미 있는가, 버전은 | `caddy version`, `apt-cache policy caddy` | 측정 전 |
| 80·443 포트가 밖에서 닿는가 | PC에서 `curl -m 5 http://<server-ip>` | 측정 전 |
| Caddy가 IP 인증서를 받는가 | `echo \| openssl s_client -connect <server-ip>:443 \| openssl x509 -noout -issuer -enddate` | 측정 전 |
| 인증서 유효기간이 약 160시간인가 | 위 명령의 `notAfter` − 발급 시각 | 측정 전 |
| **자동 갱신이 실제로 도는가** | 발급 약 3일 뒤 Caddy 로그에서 갱신 기록 확인 | **발급 직후에는 확인할 수 없다.** 3~6일 뒤 다시 본다 |
| 위조 `X-Forwarded-For` 무시 | PC에서 `X-Forwarded-For` 헤더를 붙여 `test`로 질문, 횟수가 헤더 값이 아니라 실제 IP로 세어지는지 | 측정 전 |
| SSE가 모이지 않고 바로 오는가 | 채팅 응답에서 진행 이벤트가 하나씩 도착하는지 | 측정 전 |
| `$`가 든 해시가 서버 `.env`에서 변형 없이 읽히는가 | 앱이 시작되면 통과(시작할 때 해시 형식을 검사한다) | **확인됨(2026-11-19).** 앱이 읽은 해시가 bcrypt 형식·60자이고 서비스가 시작됐다(4-1의 8·9). 실제 로그인은 Step 7에서 본다 |
| 9200 포트가 밖에서 닫혀 있는가 | PC에서 `curl -m 5 https://<server-ip>:9200` 이 실패 | 측정 전 |
| 피드백 점수 쓰기(Langfuse v3 제거 뒤) | 실제 👍 한 번 후 Langfuse에서 점수 확인 | 측정 전 |
