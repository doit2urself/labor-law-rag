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

## 4. 서버에서 실행한 명령과 결과

측정 전. (서버 작업은 명령마다 사용자 승인을 받는다.)

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
| `$`가 든 해시가 서버 `.env`에서 변형 없이 읽히는가 | 앱이 시작되면 통과(시작할 때 해시 형식을 검사한다) | 측정 전 |
| 9200 포트가 밖에서 닫혀 있는가 | PC에서 `curl -m 5 https://<server-ip>:9200` 이 실패 | 측정 전 |
| 피드백 점수 쓰기(Langfuse v3 제거 뒤) | 실제 👍 한 번 후 Langfuse에서 점수 확인 | 측정 전 |
