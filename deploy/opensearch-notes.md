# OpenSearch 서버 설치 기록 (Task 6, 2026-11-18)

서버에 OpenSearch를 설치하면서 실제로 실행한 명령과 그 결과, 겪은 문제, 확인된 사실을 적은 기록이다.
비밀번호·서버 주소·계정은 적지 않았다(프로젝트 규칙 3). 서버 이름은 `<host>`로 가렸다.

## 1. 지금 상태 (검증한 것만)

| 항목 | 값 | 확인한 방법 |
|---|---|---|
| 서버 | Ubuntu 24.04.5, x86_64, 4코어, 사용 가능 메모리 약 7.3GiB, 스왑 없음 | `uname -m`, `free -h`, `swapon --show` (읽기 전용) |
| OpenSearch | 3.10.0 (`.deb`, `dpkg` 상태 `install ok installed`) | `dpkg -s opensearch` |
| 플러그인 | `analysis-nori 3.10.0`, `opensearch-knn 3.10.0.0`, `opensearch-security 3.10.0.0` 외 | `GET /_cat/plugins?v` (사용자가 본인 터미널에서 실행) |
| 접속 주소 | `127.0.0.1`의 9200(HTTP, HTTPS+로그인), 9300(노드 통신)만 열림. `0.0.0.0`·`[::]` 없음 | `ss -ltn` |
| 서비스 | `active (running)`, 부팅 때 자동 시작(`enabled`) | `systemctl is-active/is-enabled` |
| 힙 | `-Xms1g -Xmx1g` (패키지 기본값, 수정 안 함) | `grep -nE '^-Xm[sx]' /etc/opensearch/jvm.options` |
| 시작 직후 메모리 | 서비스 `MemoryCurrent` 1,673,826,304바이트(약 1.56GiB), 서버 전체 사용 2.1GiB / 사용 가능 5.7GiB | **시작 직후 1회 측정.** 색인을 넣은 뒤 다시 재야 한다. |
| Nori 분석기 이름 | `nori`가 그대로 동작. "주휴수당을" → `주휴`, `수당` (조사 "을" 제거) | `POST /_analyze` |
| 로그인 | `admin` + 설치 때 정한 비밀번호로 성공 | 사용자가 본인 터미널에서 확인 |

## 2. 설치 방식: `.deb`로 했다

스펙 6.10은 tarball을 먼저 시도하고 `systemd` 등록이 막히면 Debian 패키지로 가라고 했다. 공식 문서에서 tarball의
`systemd` 등록 방법을 찾지 못했고, Debian 설치 문서에는 `systemctl enable|start|status opensearch`와 설정 파일
위치(`/etc/opensearch/opensearch.yml`, `/etc/opensearch/jvm.options`)가 적혀 있어서 처음부터 `.deb`를 골랐다.
스펙 10절이 "OpenSearch 설치 방식"을 빌더에게 맡겼다. 되돌리는 비용은 `apt remove --purge opensearch` 한 줄이다.

## 3. 실행한 명령 (순서대로, 명령마다 사용자 승인)

| # | 구분 | 명령 | 결과 |
|---|---|---|---|
| 1 | 읽기 | `uname -m; sysctl vm.max_map_count; swapon --show; free -h` | `x86_64`, `vm.max_map_count = 1048576`(필요한 262144보다 큼 → 이 설정은 바꾸지 않음), 스왑 없음 |
| 2 | 읽기 | `dpkg -l \| grep -i opensearch; ls /etc/opensearch /usr/share/opensearch` | 아무것도 없음(정상) |
| 3 | 읽기 | `curl -sI https://artifacts.opensearch.org/releases/bundle/opensearch/3.10.0/opensearch-3.10.0-linux-x64.deb` | HTTP 200, 크기 1,091,726,104바이트 |
| 4 | 변경 | `mkdir -p ~/opensearch-install && cd ~/opensearch-install && curl -SLO <위 주소>` | 받은 크기가 헤더와 같음 |
| 5 | 변경 | `.deb.sha512`, `.deb.sig` 내려받기 + `sha512sum` | 공식 체크섬과 128자리 모두 일치 |
| 6 | **사용자** | `read -rs` + `export OPENSEARCH_INITIAL_ADMIN_PASSWORD` + `dpkg -i` (본인 터미널) | 1차: `half-configured` (아래 4절) |
| 7 | **사용자** | `dpkg --configure opensearch` | 2차 실패, 3차 성공 |
| 8 | 변경 | `cp -a opensearch.yml opensearch.yml.bak-pre-task6` + `network.host: 127.0.0.1`, `discovery.type: single-node` 두 줄 추가 | exit 0 |
| 9 | 변경 | `/usr/share/opensearch/bin/opensearch-plugin install analysis-nori --batch` | `Installed analysis-nori` |
| 10 | 변경 | `systemctl enable opensearch && systemctl start opensearch` | `active`, `enabled` |
| 11 | 읽기 | `ss -ltn \| grep -E ':(9200\|9300)\b'` | 둘 다 `[::ffff:127.0.0.1]`(IPv4 루프백)만 |
| 12 | 변경 | 설치 로그에서 'password'가 든 2줄 삭제 `sed -i '/password/Id' …/install_demo_configuration.log` | 남은 줄 0 |

접속 계정은 서버에 `root`로 로그인하므로 문서의 `sudo`는 필요 없었다.

## 4. 겪은 문제: 설치가 `half-configured`로 멈췄다

- 증상: `dpkg -i`가 끝났는데 `dpkg -s opensearch`가 `install ok half-configured`, 서비스는 시작되지 않고 `opensearch.yml`에 보안 설정이 없음.
- 원인: 설치 후 스크립트(`/var/lib/dpkg/info/opensearch.postinst` 25행)가 보안 데모 설정(`install_demo_configuration.sh`)을 돌리는데,
  admin 비밀번호가 검증 정규식에 걸렸다. 로그 메시지: `Password does not match validation regex` —
  **8자 이상, 대문자·소문자·숫자·특수문자 각 1개 이상.** (처음 안내에서 "대문자 필수"까지만 알고 있었다. 나머지 규칙은 로그를 보고 알았다.)
- 해결: 규칙을 만족하는 새 비밀번호로 `dpkg --configure opensearch`를 다시 실행(설치는 이미 풀려 있어 마무리 단계만 반복).
- 교훈: 설치 로그(`/var/log/opensearch/install_demo_configuration.log`)에 입력한 비밀번호가 **평문으로** 찍힌다(실패 때는 규칙 위반 메시지에,
  성공 때는 "log in with admin:…" 안내문에 들어 있었다고 추정). 설치가 끝나면 그 줄을 지워야 한다. 또 사용자가 로그를 채팅에 붙이면
  비밀번호가 새므로, 이 로그는 비밀번호 부분을 가려서 읽었다.

## 5. 설정에서 알아 둘 것

- `.env`(이 PC와 서버)에 `LABOR_OPENSEARCH_PASSWORD`를 둔다. `LABOR_OPENSEARCH_URL`은 기본값 `https://127.0.0.1:9200`이고
  `LABOR_OPENSEARCH_USER`는 `admin`이다(`app/config.py`).
- 이 서버의 인증서는 설치 프로그램이 만든 **자체 서명 데모 인증서**라서 클라이언트(`make_client`)는 기본으로 검증을 끈다
  (`verify_certs=False`). 같은 서버 안(127.0.0.1) 통신이라 가로챌 길이 없다는 전제다.
- 힙은 패키지 기본 1GB 그대로다. 스펙 6.3의 "1~2GB에서 시작해 측정으로 정한다"에 맞고, 색인 뒤 다시 잰다.

## 6. 통합 테스트를 돌리는 방법 (이 PC에서, SSH 터널)

계획서는 서버에서 돌리라고 했지만, 서버에 파이썬 환경과 패키지를 올리지 않으려고 터널을 썼다(스펙 6.10 제안).

```
ssh -o BatchMode=yes -N -L 127.0.0.1:19200:127.0.0.1:9200 contabo      # 터널(창 하나를 계속 연다)
set LABOR_OPENSEARCH_URL=https://127.0.0.1:19200                        # PowerShell: $env:LABOR_OPENSEARCH_URL=...
.\.venv\Scripts\python.exe -m pytest tests/test_opensearch_integration.py -m integration -v
```

- `LABOR_OPENSEARCH_PASSWORD`가 `.env`에 없으면 8개 모두 건너뛴다.
- 테스트는 임시 색인(`labor-rag-itest-<무작위>`)과 임시 파이프라인을 만들고 끝나면 지운다. 운영 색인은 건드리지 않는다.
- 결과(2026-11-18): **8 passed (27.5초).** 이 테스트로 확인된 것:
  - faiss HNSW + `cosinesimil` 색인이 만들어진다.
  - `nori` 분석기가 `text` 필드에 적용된다.
  - RRF 파이프라인이 동작한다(점수가 하위 쿼리 순위 역수의 합이라 모두 0.06 미만).
  - 현행 필터가 미래 시행 청크를 걸러낸다.
  - 같은 청크를 다시 넣어도 문서가 늘지 않는다.
  - `delete_by_source`와 `list_source_ids`가 맞게 동작한다.
- 한계: 이 테스트의 데이터는 조문 3개와 가짜 벡터라서 **검색 품질**을 말해 주지 않는다. 정확 일치 하위 쿼리가 BM25만으로는
  못 잡는 경우를 실제로 보완하는지는 평가셋이 생기는 Task 9에서 잰다. `pagination_depth`는 서버가 받아들이고 동작함까지만
  확인했고, "하위 쿼리별 깊이가 정확히 그 값이 된다"는 것은 증명하지 않았다.

## 7. 위험과 한계

- **공개 데모 인증서와 `kirk` 관리자 인증서.** 설치 프로그램이 "공개 접속 가능한 시스템에 쓰지 말라"고 경고하는 데모 설정이다
  (`opensearch.yml`에 `plugins.security.allow_unsafe_democertificates: true`). 서버 밖에서 9200·9300에 닿지 못하는 동안에만 안전하다.
  지금은 루프백에만 열려 있다(`ss -ltn`으로 확인). 포트를 바꾸거나 `network.host`를 건드릴 때마다 다시 확인한다.
- 서버 방화벽(ufw 등)은 점검하지 않았다. 지금은 OpenSearch 자체가 루프백에만 바인드되어 있어서 방화벽 없이도 닿지 않는다.
- `.deb` 서명(`.sig`)은 검증하지 않았다(공개 키 주소를 공식 문서에서 못 찾았다). 체크섬은 같은 사이트의 것이라 파일이 깨지지 않았음만 보장한다.
- 메모리는 시작 직후 한 번만 쟀다.

## 8. 되돌리는 방법

```
systemctl stop opensearch && systemctl disable opensearch
/usr/share/opensearch/bin/opensearch-plugin remove analysis-nori
cp -a /etc/opensearch/opensearch.yml.bak-pre-task6 /etc/opensearch/opensearch.yml
apt remove --purge opensearch            # 완전 제거
rm -rf ~/opensearch-install
```
