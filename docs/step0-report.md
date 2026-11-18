# 0단계 측정 보고서 (Task 2)

- 작성 시작: 2026-11-18
- 이 문서의 수치는 모두 아래에 적힌 명령의 실제 출력이다. 측정하지 못한 항목은 "측정 전"으로 둔다.
- 서버 주소·접속 계정·키 값은 이 문서에 적지 않는다(저장소가 공개될 수 있고, 프로젝트 규칙 3).

## 1. 서버 읽기 전용 확인 (2026-11-18, SSH, 명령마다 사용자 승인)

모든 명령은 서버를 바꾸지 않는 읽기 전용이다. 출력의 서버 IP는 `<서버IP>`로 가렸다.

| 항목 | 명령 | 결과 |
|---|---|---|
| 운영체제 | `cat /etc/os-release \| head -3` | Ubuntu 24.04.5 LTS (`VERSION_ID="24.04"`) |
| 메모리 | `free -h` | 전체 7.8Gi, 사용 469Mi, **사용 가능 7.3Gi**, 스왑 0B |
| CPU | `nproc` | 4 |
| 디스크 | `df -h /` | 전체 96G, 사용 2.2G, 남음 94G (3%) |
| Docker | `command -v docker \|\| echo no-docker` | `no-docker` (설치 안 됨) |
| 외부에서 보이는 IP | `curl -4 -s https://ifconfig.me` | IPv4 응답. **SSH 접속 주소와 같음**(값은 적지 않음) |
| 열린 포트 | `ss -ltnp` | 22(sshd, IPv4·IPv6), 53(systemd-resolved, 로컬 전용 `127.0.0.53`·`127.0.0.54`)만 listen. **80·443은 비어 있음** |
| SSH 설정 | `sshd -T \| grep -Ei '^(permitrootlogin\|passwordauthentication\|pubkeyauthentication)'` | `permitrootlogin yes`, `pubkeyauthentication yes`, `passwordauthentication yes` |

### 관문(계획 Task 2 Step 7)의 서버 쪽 조건

| 조건 | 판정 | 근거 |
|---|---|---|
| 사용 가능 메모리 4GiB 미만 | 해당 없음 | 사용 가능 7.3Gi |
| OS가 Debian/Ubuntu 계열이 아님 | 해당 없음 | Ubuntu 24.04.5 LTS |
| 포트 80·443을 쓸 수 없음 | **미확인** | 두 포트에서 listen 중인 프로그램은 없다. 그러나 공급자 방화벽이나 네트워크 차단 여부는 확인하지 못했다(HTTPS 시험은 Task 18에서). |
| 무료 요금제에서 후보 모델이 하나도 호출되지 않음 | 측정 전 | 아래 2절 |

### 이후 Task에 주는 정보

- 스왑이 0B이므로 OpenSearch가 요구하는 "스왑 끄기"는 이미 충족된 상태다(Task 6).
- Docker가 없으므로 결정(Docker 미사용)과 맞는다.
- **보안 관찰:** 비밀번호 로그인(`passwordauthentication yes`)과 root 로그인(`permitrootlogin yes`)이 모두 켜져 있고 22번 포트가 인터넷에 열려 있다. 키 로그인은 이미 동작하므로 비밀번호 로그인을 끌 수 있다. 서버를 바꾸는 작업이라 사용자 승인 뒤에 별도로 진행한다(이 보고서 작성 시점에는 하지 않았다).

## 2. 무료 호출 한도 측정 (Gemini · Cohere)

측정 전 — 사용자가 이 PC의 환경 변수(`LABOR_GEMINI_API_KEY`, `LABOR_COHERE_API_KEY`)에 키를 넣은 뒤 `scripts/step0_probe.py`로 잰다.

## 3. 법제처 해석 건수

측정 전 — Task 3에서 `search_interpretations`의 `total`을 기록한다.
