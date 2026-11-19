# vLLM Remote LogKit

Kubernetes Pod 또는 SSH로 접속하는 Linux VM에서 vLLM 추론 로그를 수집하고, 브라우저에서 지연 목표에 맞는 서버 설정을 찾는 도구입니다. 분석 PC에는 Python 3.9 이상이 필요합니다. 모델과 토크나이저는 원격 장비에서 사용합니다.

## 운영 중인 Pod를 바로 확인하기

분석 PC에 Python 3.9 이상과 로그인된 `kubectl`을 준비하세요. 프로젝트 폴더에서 실행합니다. 아래 `inference`는 네임스페이스 예시입니다.

```bash
kubectl config current-context
kubectl get pods -n inference
python3 remote.py kube -n inference --pod YOUR_POD --seconds 30
```

`YOUR_POD`에 두 번째 명령에서 확인한 Pod 이름을 넣습니다. 컨테이너가 여러 개면 `-c 컨테이너이름`, vLLM 포트가 8000이 아니면 `--port 포트번호`를 붙이세요. PC에서 `web/index.html`을 열고 **원격 수집 → Pod 관측 파일 열기**에서 명령이 출력한 `kubectl-summary.json`을 선택합니다.

| 하려는 일 | 명령 | HTML에서 확인할 내용 |
|---|---|---|
| 운영 Pod가 밀리는지 확인 | `kube` | 대기·실행 요청, KV 캐시 사용률, 선점·로그. 추가 추론 요청 0건 |
| 현재 배포의 지연·처리량 측정 | `collect` | 지정한 부하의 p95 지연, 초당 출력 토큰 |
| 서버 설정 후보 비교 | 전용 장비에서 `launch` | 같은 부하와 지연 목표에서 처리량이 가장 높은 실측 설정 |

권한 확인, 로그 전용 모드와 지표 해석 예시는 [kubectl 빠른 시작](docs/kubectl-quickstart.md)에 있습니다.

## 장비 없이 시작하기

저장소 루트에서 아래 두 명령을 실행하면 예시 벤치마크를 대시보드용 CSV로 변환합니다. Python 표준 라이브러리만 사용합니다. Windows에서는 `python3`을 `py -3`으로 바꿀 수 있습니다.

```bash
python3 remote.py extract --raw examples/bench.json --meta examples/meta.json --out imports/demo/html.json
python3 remote.py merge imports --out imports/demo.csv
```

브라우저에서 `web/index.html`을 열고 **측정 파일 열기**에서 `imports/demo.csv`를 선택합니다. 예시의 정합성 상태는 `unknown`입니다. 추천 설정까지 확인하려면 **정합성 통과 설정만 추천**을 해제합니다.

## 설정 파일로 Pod·VM에서 벤치마크하기

`configs/targets.example.json`을 `configs/targets.json`으로 복사합니다. Pod를 대상으로 할 때는 context·namespace·Pod·컨테이너, VM을 대상으로 할 때는 SSH 별칭을 입력합니다. 모델 ID, 칩 수, 토크나이저 경로, vLLM 상한은 실제 배포 값으로 채웁니다.

| 명령 | 사용 시점 | 동작 |
|---|---|---|
| `observe` | 서비스의 기존 트래픽 관측 | 지정한 시간 동안 엔진 메트릭 샘플링 |
| `collect` | 실행 중인 모델의 처리량·지연 측정 | 현재 설정으로 워밍업 후 벤치마크 실행 |
| `launch` | 전용 장비에서 서버 설정 비교 | 설정별 모델 기동, 벤치마크, 프로세스 종료 |

```bash
# 계획만 확인. 원격 장비에 접속하지 않습니다.
python3 remote.py collect --target ironwood-pod --setting baseline --plan

# 실제 측정. VM을 대상으로 할 때는 설정 파일의 VM 대상 ID를 지정합니다.
python3 remote.py collect --target ironwood-pod --setting baseline
```

측정이 끝나면 `results/<대상ID>-<작업ID>/runs.csv`를 HTML에서 엽니다. 상세 명령과 로그 열의 정의는 다음 문서에 있습니다.

- [Pod·VM 실행 가이드와 커널 Roofline 예제](docs/usage.md)
- [수집 시점·환경값·로그 데이터 사전](docs/data-contract.md)
- [다중 모델 및 GPU·TPU 설정 예시](configs/targets.example.json)

## 저장소 구조

| 경로 | 용도 |
|---|---|
| `remote.py` | 분석 PC의 CLI. 실행 계획, 원격 작업 시작·회수, CSV 변환과 병합 |
| `kube_observe.py` | PC의 kubectl로 포트 포워딩·로그 조회, 관측 JSON 생성 |
| `worker.py` | 원격 장비의 모델 기동, 부하 생성, 메트릭 수집 |
| `analyze_logs.py` | 메트릭 요약 및 프로파일러 측정값의 Roofline 단위 변환 |
| `web/index.html` | 브라우저에서 실행하는 분석 화면 |
| `configs/`, `examples/` | 접속 설정 양식과 가상 측정 데이터 |
| `docs/`, `tests/` | 상세 사용법, 데이터 사전, 모의 Pod·VM 회귀 테스트 |

## 테스트

Linux 또는 macOS에서 아래 명령을 실행합니다. 테스트가 모의 SSH·kubectl·vLLM 환경을 구성하므로 GPU·TPU나 클러스터 계정이 필요하지 않습니다.

```bash
python3 -m unittest discover -s tests -v
```

