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
| 무료 요금제에서 후보 모델이 하나도 호출되지 않음 | 해당 없음(5개 모두 호출됨) | 아래 2절. 다만 이 키가 무료 프로젝트의 것인지는 확인하지 못했다. |

### 이후 Task에 주는 정보

- 스왑이 0B이므로 OpenSearch가 요구하는 "스왑 끄기"는 이미 충족된 상태다(Task 6).
- Docker가 없으므로 결정(Docker 미사용)과 맞는다.
- **보안 관찰과 조치(해결됨):** 처음에는 비밀번호 로그인(`passwordauthentication yes`)과 root 로그인(`permitrootlogin yes`)이 모두 켜져 있고 22번 포트가 인터넷에 열려 있었다. 원인과 조치는 아래와 같다(명령마다 사용자 승인, 2026-11-18).
  - 원인: sshd는 `sshd_config.d/*.conf`를 번호 순으로 읽고 **먼저 나온 값이 이긴다**. `50-cloud-init.conf`가 `PasswordAuthentication yes`, 뒤의 `60-cloudimg-settings.conf`가 `no`라서 50번이 이겼다. `PermitRootLogin yes`는 본 설정 `sshd_config` 42번 줄에 있었다.
  - 조치: cloud-init이 관리하는 파일은 건드리지 않고 `/etc/ssh/sshd_config.d/00-hardening.conf`를 추가했다(내용: `PasswordAuthentication no`, `PermitRootLogin prohibit-password`). 이름이 00이라 먼저 읽힌다.
  - 적용 전 확인: `sshd -t` 문법 정상(exit 0), `sshd -T`가 `passwordauthentication no`·`permitrootlogin without-password`·`kbdinteractiveauthentication no`·`pubkeyauthentication yes`를 보여 줌. root에 등록된 키는 하나이고 지문이 이 PC 에이전트의 키와 같음.
  - 적용: `systemctl reload ssh` (열린 접속은 끊기지 않음).
  - 적용 후 확인: 키로 새 연결 성공(`echo key-login-ok`), `ssh.service`·`ssh.socket` 모두 active. 클라이언트 쪽 `ssh -v`의 `Authentications that can continue`가 적용 전 `publickey,password` → 적용 후 `publickey`로 바뀜.
  - **되돌리는 방법:** 서버에서 `rm /etc/ssh/sshd_config.d/00-hardening.conf && systemctl reload ssh`. 접속이 아예 안 되면 Contabo 고객 패널의 VNC 콘솔로 같은 명령을 실행한다(콘솔 접근은 확인하지 못함).
  - 이 PC 쪽 설정: 별칭 `contabo`(`~/.ssh/config`), 키는 `ssh-agent`가 보관(서비스 자동 시작). `~/.ssh` 폴더 권한은 `GeneralUser`를 뺀 세 곳으로 좁힘.

## 2. 무료 호출 한도 측정 (Gemini · Cohere)

측정일 2026-11-18. 키는 사용자가 `.env`에 넣었고(값은 대화·문서에 없음), `scripts/step0_probe.py`가 읽었다. 호출은 모델마다 순서대로 최대 12회, 사이에 대기 없음(각 호출은 직전 호출이 끝난 뒤에 시작). 명령은 `python scripts/step0_probe.py <종류> --model <모델> --max-calls 12`.

| 종류 | 모델 | calls_ok | first_429_at | elapsed_s | retry_after_s |
|---|---|---|---|---|---|
| gemini-chat | gemini-3.5-flash-lite | 12 | 없음 | 12.48 | 없음 |
| gemini-chat | gemini-3.1-flash-lite | 12 | 없음 | 11.72 | 없음 |
| gemini-chat | gemini-3.8-flash | 12 | 없음 | 21.88 | 없음 |
| gemini-chat | gemini-3.6-flash | 12 | 없음 | 22.31 | 없음 |
| gemini-chat | gemini-4-hydrogen | 12 | 없음 | 29.20 | 없음 |
| gemini-embed | gemini-embedding-2 | 12 | 없음 | 5.61 | 없음 |
| cohere-rerank | rerank-v5.0-pro | **10** | **11** | 6.84 | 없음 |

### 읽는 법과 한계

- **Cohere:** 10번 성공한 뒤 11번째에서 429가 났고, 전체가 6.84초 안이었다. 기억해 둔 "시험 키 Rerank 분당 10회"와 맞아떨어진다. 모델 이름 `rerank-v5.0-pro`는 Cohere 공식 문서(`docs.cohere.com/docs/rerank-overview`)의 모델 목록에 그대로 있었다(Task 8의 이름 추측이 맞음). 429에 대기 시간 정보는 오지 않았다(`retry_after_s` 없음). 이번 측정으로 월 호출 한도 1,000회 중 약 11~12회를 썼다(429 호출이 세어지는지는 모른다).
- **Gemini 5개 모델과 임베딩:** 12번 안에서는 한 번도 429가 나지 않았다. 이것은 "한도가 12회보다 크다"까지만 말해 준다. **분당·일일 한도의 실제 숫자는 이번 측정으로 알 수 없다**(상한을 12로 둔 설계였고 한도에 닿지 않음). 그래서 Task 7의 색인 예산(해석 전량 임베딩이 하루에 들어가는가)은 아직 계산할 수 없다. 이 값은 Task 7에서 대량 임베딩을 하며 429가 나는 지점을 직접 보고 정하거나, 한도를 더 크게 잡은 측정을 따로 하면 알 수 있다.
- **키가 무료 요금제의 것인지는 이번 측정이 보여 주지 않는다.** 호출이 성공했다는 사실만으로는 무료 프로젝트인지 결제가 연결된 프로젝트인지 구분되지 않는다. 월 청구액 0원 조건을 지키려면 사용자가 Google AI Studio에서 이 키의 프로젝트가 무료(Free tier)로 표시되는지 확인해야 한다.
- 호출 한 건의 입력은 짧은 시험 문장이다. 실제 조문 길이(수천 자)에서는 분당 토큰 한도가 먼저 닿을 수 있다 — 이것은 가능성일 뿐 측정한 것이 아니다.
- Gemini의 429 응답에서 `retryDelay`를 읽는 코드는 Google의 RetryInfo 형식을 가정하고 만들었다. 이번에 Gemini 429가 한 번도 나지 않아서 **실제 응답으로는 아직 검증되지 않았다.**

## 3. 법제처 해석 건수

측정 전 — Task 3에서 `search_interpretations`의 `total`을 기록한다.
