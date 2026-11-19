# kubectl로 실행 중인 vLLM Pod 관측하기

분석 PC에서 Pod 하나의 대기 요청, 실행 요청, KV 캐시 사용률, 로그를 확인하는 방법입니다. 추론 요청을 추가하지 않으므로 운영 중인 모델에도 사용할 수 있습니다. 예시에서 네임스페이스는 `inference`, 컨테이너는 `vllm`, 모델 서버의 HTTP 포트는 `8000`입니다. 실제 배포 값으로 바꾸세요.

명령을 직접 작성하기 어려우면 먼저 `web/index.html`의 **원격 수집**을 여세요. 네임스페이스와 Pod 이름을 입력하면 터미널에 복사할 명령이 표시됩니다.

## 1. 어느 Pod를 볼지 정합니다

PC에 Python 3.9 이상과 클러스터에 로그인된 `kubectl`이 필요합니다. 프로젝트 폴더에서 아래 명령을 실행하세요. Windows에서는 `python3` 대신 `py -3`를 사용할 수 있습니다.

```bash
kubectl config current-context
kubectl get pods -n inference
```

첫 줄에서 **접속하려는 클러스터의 context**를 확인합니다. 두 번째 명령 결과에서 모델이 실행 중인 `Running` Pod의 `NAME`을 복사합니다. 예를 들어 이름이 `llm-server-7d9b6f84f9-abcde`이면 다음 단계의 `YOUR_POD` 자리에 그 이름을 넣습니다. Pod 이름을 이미 알고 있으면 목록 조회는 생략해도 됩니다.

## 2. 평소 트래픽이 있을 때 30초간 관측합니다

```bash
python3 remote.py kube -n inference --pod YOUR_POD --seconds 30
```

PC의 수집기가 해당 Pod로 임시 포트를 연결해 기본 2초 간격으로 `/metrics`를 읽고, 관측 시작 이후의 컨테이너 로그를 가져옵니다. 포트는 PC의 `127.0.0.1`에만 열리고 수집 종료 시 닫힙니다. Pod 안에는 파일을 복사하지 않습니다. [Kubernetes 포트 포워딩](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_port-forward/) · [컨테이너 로그](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_logs/)

| 배포 상황 | 실행할 명령 또는 추가 옵션 |
|---|---|
| 컨테이너가 여러 개 | `kubectl get pod YOUR_POD -n inference -o jsonpath='{.spec.containers[*].name}'`으로 이름 확인 후 `-c vllm` 추가 |
| 서버가 8080 포트에서 동작 | `--port 8080` 추가. Service가 외부에 공개한 포트 대신 Pod에서 vLLM이 듣는 포트를 지정 |
| 다른 클러스터를 지정 | `--context MY_CONTEXT` 추가. Pod 조회도 같은 context에서 수행 |
| 로그만 필요하거나 포트 포워딩 권한이 없음 | `--logs-only` 추가. 지정한 시간 동안의 로그와 관련 문구의 줄 수 확인 |
| 여러 replica 또는 여러 모델 | Pod별로 명령을 실행. 실행마다 별도 결과 폴더를 만들며 HTML에서 모델·엔진 라벨을 표시 |

## 3. 브라우저에서 결과를 봅니다

1. 명령 마지막에 나온 `results/kube-…/kubectl-summary.json` 경로를 확인합니다.
2. 압축을 푼 폴더에서 `web/index.html`을 열고 **원격 수집 → Pod 관측 파일 열기**를 누릅니다.
3. 방금 생성한 JSON을 선택합니다. 상단에 표시한 context·Pod·컨테이너·UTC 시각으로 대상을 확인합니다. 먼저 연습하려면 `examples/kubectl-summary.json`을 선택하세요.

| 어디를 볼까 | 읽는 방법 |
|---|---|
| 대기·실행 요청 | 대기는 처리 순서를 기다리는 요청 수입니다. 예를 들어 대기 최대 12건, 마지막 0건이면 중간에 생긴 대기가 마지막 표본에서는 해소된 상태입니다. |
| KV 캐시 사용률 | 엔진이 잡아 둔 KV 캐시 블록 중 사용 중인 비율입니다. 원시 값 0.80은 화면에서 80%입니다. 대기가 지속되면 같은 시각의 캐시 사용률과 선점 증가량을 함께 확인합니다. |
| 생성 토큰·선점 증가량 | 같은 모델·엔진의 첫 표본부터 마지막 표본까지 누적 카운터가 얼마나 늘었는지 봅니다. 표본이 1개면 증가량은 빈칸입니다. 리셋이 있으면 두 표본 사이의 일부 증가는 알 수 없습니다. |
| 로그·OOM 문구 | 관측 구간에서 가져온 줄 가운데 관련 문구가 있는 줄 수입니다. 원문은 같은 폴더의 `pod.log`, 발췌는 `events.log`에 있습니다. OOMKilled 원인·종료 상태는 별도로 Pod 상태에서 확인합니다. |

대기 최대치와 캐시 최대치는 서로 다른 시각에 나올 수 있습니다. 동시에 일어났는지는 `metrics.jsonl.gz`의 UTC 시각을 맞춰 확인하세요. 여러 Pod·엔진을 합산하거나 한 Pod의 데이터를 전체 서비스 값으로 확대하지 않습니다. [vLLM 엔진 메트릭 정의](https://docs.vllm.ai/en/stable/usage/metrics/)

## 연결이 안 될 때

| 보이는 현상 | 확인할 항목 |
|---|---|
| `kubectl`을 찾을 수 없음 / 로그인 오류 | PC 터미널에서 `kubectl config current-context`와 `kubectl get pod YOUR_POD -n inference`부터 확인 |
| `Forbidden` | 해당 네임스페이스의 권한 확인. 대상 조회는 `kubectl auth can-i get pods -n inference`, 로그는 `kubectl auth can-i get pods --subresource=log -n inference`, 포트 연결은 `kubectl auth can-i create pods --subresource=portforward -n inference` |
| `/metrics HTTP 401/403/404`, 연결 거절, 지표 없음 | Pod의 HTTP 포트와 메트릭 노출 여부를 확인. 이 명령은 인증 헤더가 필요 없는 Pod 내부 HTTP 경로를 사용합니다. 필요하면 로그 전용 모드로 확인 |
| 일부 수집 | 로그나 메트릭 중 읽을 수 있었던 결과와 오류 사유를 함께 표시. 수집하지 못한 값은 0으로 채우지 않음 |
| 분석 제외 | Pod·컨테이너 교체, 종료 시 식별 실패 또는 사용 가능한 결과 없음. 화면의 사유를 확인한 뒤 현재 Pod로 다시 실행 |

로그는 조회 시점 기준 **최근 최대 2,000줄·2 MiB**입니다. 전체 로그 보관이나 장애 횟수 집계가 목적이면 중앙 로그 시스템을 사용하세요.

## 언제 벤치마크와 Roofline으로 넘어갈까

대기 요청이 계속 쌓이면 테스트 Pod에서 같은 모델·입출력 길이·동시 요청 수를 고정합니다. `collect`로 현재 설정을 측정하거나 `launch`로 서버 상한을 하나씩 바꿉니다. HTML **서빙 분석**에서 p95 TTFT(첫 토큰 지연), p95 ITL(토큰 간 지연) 목표를 만족한 설정 중 처리량이 높은 후보를 확인합니다. **커널 Roofline**으로 원인을 좁힐 때는 같은 연산 구간의 FLOP, HBM 읽기·쓰기 바이트, 실행 시간을 프로파일러에서 가져옵니다. [벤치마크와 Roofline 실행 예시](usage.md)
