# vLLM Remote LogKit

Kubernetes Pod나 Linux VM에서 vLLM의 지연과 처리량을 측정하고, HTML 대시보드에서 **서비스 지연 목표를 만족하는 설정 중 초당 출력 토큰이 가장 많은 설정**을 찾는 도구입니다. 운영 중인 Pod의 대기 요청과 KV 캐시도 따로 관측할 수 있습니다.

## 3분 실습 · 서버 없이 결과부터 보기

1. 압축을 풀고 `vllm-remote-logkit/web/index.html`을 브라우저에서 엽니다. **서빙 분석**에 보이는 첫 화면은 가상 TPU 예시입니다.
2. **측정 파일 열기**에서 `examples/mac-smoke.csv`를 선택합니다. **설정별 측정 결과**에 맥북 CPU 실측값인 첫 응답 **286.8ms**, 출력 **18.91토큰/초**가 표시됩니다. 이 파일은 한 가지 설정을 측정한 기준값입니다.
3. **데이터 가이드 → 예시로 돌아가기**를 누릅니다. 동시 요청 수 `1024`, 첫 토큰 지연 목표 `1500ms`에서 토큰 간 지연 목표를 `50ms`에서 `45ms`로 낮춰 보세요. 추천 토큰 예산이 `8192`에서 `4096`으로 바뀝니다. [수치와 판정 근거](docs/reading-results.md)

HTML은 선택한 파일을 브라우저에서 읽습니다. 파일 분석에는 Python 설치나 별도 웹 서버가 필요하지 않습니다.

## 목적별 실행 방법

아래 명령은 프로젝트 루트에서 실행합니다. 수집 PC에는 Python 3.9 이상이 필요합니다. Windows에서는 `python3` 대신 `py -3`을 쓸 수 있습니다.

| 하려는 일 | 실행 위치와 준비물 | 결과 |
|---|---|---|
| 맥북에서 실모델로 흐름 확인 | macOS, GGUF 텍스트 모델과 호환 `llama-server`. `python3 mac_smoke.py` | 로컬 서버 기동 → 워밍업 → 실측 5건 → 종료. `runs.csv` 생성 |
| 운영 Pod의 기존 트래픽 관측 | PC에 인증된 `kubectl`. `python3 remote.py kube -n inference --pod YOUR_POD --seconds 30` | `kubectl-summary.json`. HTML **원격 수집 → Pod 관측 파일 열기**에서 확인 |
| 기존 vLLM 서버 부하 측정 | SSH VM 또는 Pod에 원격 Python·vLLM·토크나이저 준비. `collect` 실행 | 실제 요청을 보내고 `runs.csv` 회수. 기존 서버는 계속 동작 |
| 전용 장비에서 설정별 모델 기동 | 같은 원격 준비물과 가중치, 빈 포트·가속기. `launch` 실행 | 설정별로 서버 기동·측정·종료. HTML에서 설정 간 비교 |
| 기존 결과 재사용 또는 커널 분석 | 벤치 JSON과 실행 메타데이터는 `extract`, 여러 결과는 `merge`. 프로파일러 수치는 `analyze_logs.py roof` | 서빙 입력 CSV 또는 커널 Roofline 계산용 수치 |

**원격 실측 예시**에는 Qwen3-8B의 모델 리비전, 입력·출력 길이, 워밍업과 반복 조건이 들어 있습니다. H200은 실제 SSH 별칭과 Python·vLLM 경로, Ironwood는 context·namespace·Pod·컨테이너·칩 수·TP 값을 먼저 채우세요. VM·Pod의 모델 경로와 실제 서버 빌드도 확인합니다. [모델 준비와 기동 명령 전체](docs/model-serving.md)

```bash
cp -n configs/qwen3-8b.example.json configs/qwen3-8b.json
# 복사한 파일을 실제 환경에 맞춘 후 실행합니다.
python3 remote.py launch --config configs/qwen3-8b.json --target h200-vm --setting baseline --scenario first-run --plan
python3 remote.py launch --config configs/qwen3-8b.json --target h200-vm --setting baseline --scenario first-run
```

`--plan`은 접속 없이 실행 범위를 확인합니다. 위 실측은 워밍업 2건과 본 측정 8건을 실행합니다. TPU Pod의 대상 ID는 `ironwood-pod`입니다. 이미 띄운 서버는 실제 기동 설정과 일치하는 `--setting`을 지정해 `collect`로 연결합니다.

## 산출물과 상세 문서

| 파일 또는 폴더 | 포함한 내용 |
|---|---|
| [web/index.html](web/index.html) | 지연 목표·처리량 비교, 한 변수의 효과, 동시 요청 수별 변화, 커널 Roofline, Pod 관측 결과 |
| [remote.py](remote.py) · [worker.py](worker.py) | PC에서 실행하는 CLI와 원격 장비에서 모델·부하·메트릭을 관리하는 수집기. `fetch`로 원격 작업 회수 가능 |
| [kube_observe.py](kube_observe.py) · [kubectl 빠른 시작](docs/kubectl-quickstart.md) | Pod 선택, 포트 포워딩, 로그 전용 모드, 권한 확인, 관측 중 Pod 교체 감지 |
| [mac_smoke.py](mac_smoke.py) · [맥북 실측 가이드](docs/mac-smoke.md) | 로컬 모델 선택, 생성 응답과 소요 시간 확인, 맥북에서 얻은 실측값 |
| [analyze_logs.py](analyze_logs.py) · [Pod·VM 및 프로파일 실행 가이드](docs/usage.md) | 엔진 시계열 요약, 기존 벤치 파일 재사용, FLOP·HBM 바이트·시간의 단위 변환 |
| [수집 데이터 사전](docs/data-contract.md) | 환경값·로그 출처, 수집 시점, 필수 CSV 열과 단위, 실패·결측 처리 |
| [결과 해석 실습](docs/reading-results.md) | 실제 CSV 입력, 가상 설정 3개 비교, Pod 관측과 커널 Roofline 읽기 |
| [일반 대상 설정](configs/targets.example.json) · [Qwen3-8B 설정](configs/qwen3-8b.example.json) | 여러 모델·장비·시나리오의 접속 및 측정 설정 양식 |
| [examples](examples) · [tests](tests) | 맥북 실측 CSV, 가상 Pod·벤치·프로파일 데이터, 모의 Kubernetes·SSH 회귀 테스트 |

## 외부 Git에 올리기

**새 저장소를 시작할 때**는 `vllm-remote-logkit-source.zip`을 받으세요. ZIP 안의 `vllm-remote-logkit/`에 Git 추적 대상인 소스·문서·예제가 들어 있습니다. 압축을 푼 상위 폴더에서 다음을 실행합니다. `REMOTE_URL`을 비어 있는 외부 저장소의 HTTPS 또는 SSH 주소로 바꾸세요.

```bash
cd vllm-remote-logkit
git init -b main
git add .
git status --short
git commit -m "Import vLLM Remote LogKit"
git remote add origin REMOTE_URL
git push -u origin main
```

**기존 커밋 이력을 이어갈 때**는 `vllm-remote-logkit_git.zip`을 받으세요. 압축을 푼 위치에 같은 소스 폴더와 `vllm-remote-logkit.bundle`이 있습니다. 아래 방식은 이전 커밋과 그 당시 문서 내용도 함께 전송합니다.

```bash
git clone -b main ./vllm-remote-logkit.bundle vllm-remote-logkit-work
cd vllm-remote-logkit-work
git remote set-url origin REMOTE_URL
git push -u origin main
```

**이미 커밋이 있는 외부 저장소**에 넣는 경우에는 그 저장소를 먼저 복제하고 작업 브랜치를 만드세요. 소스 ZIP에서 꺼낸 `vllm-remote-logkit/`을 원하는 하위 폴더로 복사한 뒤, 그 폴더만 커밋하면 기존 이력을 유지할 수 있습니다.

실제 접속 설정(`configs/qwen3-8b.json` 등), `results/`, 환경 파일, 개인 키와 모델 가중치는 기본 제외 대상입니다. 개인 설정은 예시 파일을 복사한 뒤 작성하세요. [Git 제외 규칙](.gitignore)

## 검증 범위

- **맥북 실측** — Apple M5 Max·36GB, llama.cpp CPU 4스레드, `gemma4:e4b`. 한 설정에서 입력 11토큰·출력 12토큰, 순차 요청 5건을 완료했습니다.
- **회귀 테스트** — Linux 또는 macOS에서 `python3 -m unittest discover -s tests -v`를 실행합니다. 모의 SSH·kubectl·vLLM으로 수집, 실패 처리, 회수를 검사합니다.
- **원격 장비** — H200 VM과 Ironwood GKE용 실행 계획을 확인했습니다. 실제 처리량·지연·정합성은 대상 클러스터에서 측정해야 합니다.
