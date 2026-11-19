# vLLM 원격 로그 수집과 Roofline 분석

브라우저를 여는 PC에 모델, 토크나이저, GPU, vLLM을 설치할 필요가 없다. 운영 Pod의 기존 트래픽만 확인할 때는 [kubectl 빠른 시작](kubectl-quickstart.md)을 따른다. 이때 Pod 안에 Python이 필요하지 않다. 아래의 설정 파일 기반 경로에서는 PC의 Python 프로그램이 지정한 Kubernetes Pod 또는 SSH VM에 수집기를 전송한다. 원격 수집기가 vLLM API와 같은 위치에서 부하를 발생시키고 메트릭을 읽는다. 결과를 PC로 가져온 뒤 `web/index.html`에서 비교한다. 측정 지연은 **원격 부하 생성기에서 서버까지의 지연**이다. 실제 사용자와 GKE 인그레스 사이의 네트워크 지연은 별도 실험으로 측정한다.

## 시작하기

| 위치 | 준비물 |
|---|---|
| 분석만 하는 PC 또는 정적 웹 서버 | 최신 브라우저와 `web/index.html`. HTML만 복사하거나 정적 호스팅해도 결과 파일을 브라우저에서 분석한다. |
| 수집 명령을 실행하는 PC | Python 3.9 이상. Pod에 연결할 때는 인증된 kubectl, VM에 연결할 때는 SSH 클라이언트와 등록된 호스트 별칭. Python 외부 패키지는 필요 없다. Windows에서는 아래 `python3`을 `py -3`로 바꿀 수 있다. |
| 원격 수집기를 실행할 Pod 또는 VM | Linux, Python 3.9 이상, 임시 디렉터리 쓰기 권한. `collect`와 `launch`에는 해당 플랫폼용 vLLM CLI와 고정된 토크나이저가 필요하다. `observe`는 /metrics를 읽을 수 있으면 된다. |
| 권한·경로 | Pod 조회는 `get pods`, 실행은 `create pods/exec`, 컨테이너 로그는 `get pods/log` 권한을 사용한다. SSH는 비대화식 인증을 사용한다. 원격 Python은 vLLM을 설치한 가상환경 경로를 지정하는 편이 버전 추적에 유리하다. |

1. 압축을 푼 뒤 프로젝트 루트에서 `configs/targets.example.json`을 `configs/targets.json`으로 복사한다. Linux나 macOS에서는 `cp configs/targets.example.json configs/targets.json`, PowerShell에서는 `Copy-Item configs/targets.example.json configs/targets.json`을 실행한다.
2. 실제 모델 ID, 모델·토크나이저 리비전, 원격 파일 경로, 장비 수, TP/DP, 이미지 버전, 양자화와 고정 옵션을 채운다. `served_name`은 대상의 `/v1/models`에 나타나는 ID다. Pod의 이름, 네임스페이스, 컨테이너는 실제 배포 정보로 바꾼다. VM의 `connection.host`는 분석 PC의 SSH config 별칭이다. 서비스 인증이 필요하면 원격 작업 프로세스에서 쓸 수 있는 `OPENAI_API_KEY`를 준비한다.
3. `--plan`으로 대상 수와 반복 횟수를 본 후 실행한다. 예시 설정에서는 모델 하나·서버 설정 하나당 동시 요청 수 2개 × 2회 반복으로 **4회** 측정한다.
4. 수거가 끝나면 PC의 `results/<대상ID>-<작업ID>/runs.csv`를 브라우저의 **측정 파일 열기**에서 연다. 자체 정합성 평가 전에는 `quality=unknown`이다. 성능 탐색만 할 때에는 **정합성 통과 설정만 추천**을 해제한다.

## 기존 서비스 관측과 벤치마크

```bash
# 기존 트래픽이 있을 때 메트릭 60초 수집. 추가 추론 요청은 0건
python3 remote.py observe --target ironwood-pod --seconds 60

# 이미 서비스 중인 Pod의 현재 설정. 두 번째 명령이 실제 부하를 발생시킨다.
python3 remote.py collect --target ironwood-pod --setting baseline --plan
python3 remote.py collect --target ironwood-pod --setting baseline

# VM 위 vLLM 서비스에 같은 워크로드 적용
python3 remote.py collect --target h200-vm --setting baseline

# 설정과 동시 요청 수가 같은 두 대상을 순차 측정
python3 remote.py collect --target ironwood-pod --target h200-vm --setting baseline

# 전송이 끊긴 경우 출력된 경로로 다시 접속. 원격 벤치마크를 재시작하지 않는다.
python3 remote.py fetch results/TARGET-JOB/receipt.json

# 완료된 여러 실험을 합쳐 HTML에서 연다.
python3 remote.py merge results --out results/all-runs.csv
```

Pod 안에서는 `127.0.0.1`의 모델 API를 사용한다. Service 주소를 통해 서로 다른 replica의 부하와 메트릭이 섞이는 상황을 막기 위한 동작이다. 원격 작업 시작·회수 전후의 Pod UID, container ID, 이미지 ID, 재시작 횟수를 대조한다. Pod를 교체했다면 새 측정으로 기록한다. 동일 Pod에서 서버 프로세스만 교체한 경우에는 기동 로그와 시각도 확인한다. 하나의 VM에 여러 모델이 있으면 각 포트·served_name을 별도 대상 ID로 등록한다.

`collect`는 기존 프로세스의 설정을 변경하지 않는다. 셋업에 여러 후보가 있으면 현재 설정 하나를 `--setting`으로 지정한다. 서버가 알리는 모델 ID와 문맥 길이를 대조한다. VM/Pod 내부의 실제 서버 PID를 알면 대상에 `"server_pid": 1234`를 추가한다. Linux /proc에서 공개 가능한 옵션과 환경변수만 추출하고 명시적인 숫자 상한을 명세와 대조한다. 자동 결정됐거나 CLI에 없는 값은 배포 명세와 로그로 보완한다.

## 전용 장비에서 서버를 새로 띄워 튜닝하기

```bash
# 모델을 시작할 원격 VM의 전용 가상환경 및 빈 포트를 먼저 지정한다.
python3 remote.py launch --target h200-vm --plan
python3 remote.py launch --target h200-vm

# 한 설정만 확인한다. TPU용 vLLM을 갖춘 전용 Pod에서도 같은 명령을 사용한다.
python3 remote.py launch --target ironwood-pod --setting tokens4096
```

`launch`는 원격 대상의 기존 컨테이너/VM 안에서 `vllm serve`를 실행한다. Kubernetes Deployment/Pod 생성은 기존 클러스터의 배포 절차를 사용한다. 모델 파일은 원격 `model_path`, 토크나이저는 원격 `tokenizer_path`에서 읽는다. `server_args` 배열에는 백엔드별 추가 인자를 넣을 수 있다. 실행 중인 모델이 쓰는 포트에서 시작하면 해당 설정을 실패로 기록한다. 기본 주소는 127.0.0.1이므로 운영 서비스용 외부 바인딩이 필요할 때는 기존 배포를 `collect`로 관측한다.

설정마다 서버 기동 → 모델 ID 확인 → 조합별 별도 워밍업 → 측정 중 /metrics 샘플링 → 결과 기록 → 소유한 서버·worker 프로세스 정리 순서다. 기동 실패 시 해당 조합의 벤치 결과를 생성하지 않는다. `launch.json`, `ready.json`, `server.log`에서 기동 인자, ready까지의 시간, 캐시·컴파일 로그를 대조한다. 정확한 prefill·decode 분리는 별도 프로파일이 필요하다.

## 모델 추가와 실험 조건

`targets`에 같은 형식의 항목을 추가하고 고유한 `id`를 부여한다. H100, H200, Ironwood v7, 칩 수가 다른 구성과 백엔드 빌드는 각기 다른 환경으로 등록한다. `scenarios`에서 입력·출력 토큰 수, 동시 요청 수 목록, 요청 수, 워밍업 수, 반복과 시드를 정의한다. 수집기는 고정 길이 random 데이터로 OpenAI 호환 스트리밍 텍스트 completions를 측정한다. 음성·임베딩·모델별 특수 입력에는 각 태스크용 부하 생성기와 지표가 필요하다.

`--target`을 생략하면 설정 파일의 모든 대상을 순차 실행한다. 여러 서버 설정에서 파라미터 하나만 바꾸면 HTML의 한 변수 비교가 가능하다. 예시는 64/1024 → 64/4096 → 256/4096이다. 4096/8192도 후보로 넣을 수 있지만 실제 기동·메모리·서비스 지연을 확인한 뒤 판단한다. 같은 환경 이름에 KV 캐시 dtype, prefix caching, chunked prefill, 컴파일 버킷이 섞이지 않게 `fixed_options`에 실제 값을 기입한다. 태그가 재사용되는 이미지에는 이미지 digest를 기록한다.

## 기존 로그와 결과 JSON 재사용

- 이미 저장된 `vllm bench serve` 결과가 있으면 가속기를 다시 가동할 필요가 없다. `examples/meta.json`을 복사해 당시 조건·고유 run_id를 입력하고 실제 데이터는 `data_kind=measured`로 설정한다. `--metric-percentiles 95,99 --percentile-metrics ttft,itl --save-result`로 저장된 필드를 사용한다. 분포가 없는 평균·p99에서 p95를 복원할 수 없다.
- VM에서 파일로 남는 서버 로그는 `server_log`에 원격 경로를 입력한다. systemd 서비스면 `journal_unit`에 유닛명을 입력한다. 컨테이너에서는 해당 이름 공간에서 접근 가능한 경로가 필요하다. Kubernetes는 회수 시점에 지정 컨테이너의 최근 20,000줄을 가져온다. 로그 회전 이전 데이터는 별도 중앙 로깅에서 확보한다.
- 완전히 중단된 원격 작업은 `remote.py fetch RECEIPT --partial`로 진단 자료만 회수한다. 원격 작업은 임시 디렉터리에 남으므로 VM 재부팅·Pod 소멸 시 일부 자료가 사라질 수 있다. `connection.tmp`에 쓰기 가능한 영속 볼륨을 지정할 수 있다. PC와 원격에 생성된 실제 로그는 Git 추적에서 제외한다.

```bash
# 장비 없이 변환·HTML 입력 흐름을 확인하는 합성 예시
python3 remote.py extract --raw examples/bench.json --meta examples/meta.json --out imports/demo/html.json
python3 remote.py merge imports --out imports/demo.csv

# 단일 run의 서버 메트릭을 엔진·모델 라벨별로 요약
python3 analyze_logs.py --out results/metrics-summary.json metrics PATH/metrics.jsonl.gz
```

## 하드웨어 Roofline

같은 연산·같은 칩 범위에서 프로파일러로 총 FLOP, HBM 읽기+쓰기 총 바이트, 시간, 정밀도 기준 peak TFLOP/s, HBM TB/s를 확보한다. GPU는 Nsight Compute의 커널 리포트, TPU는 해당 백엔드가 지원하는 XProf의 HBM 계층·연산별 자료를 사용한다. XProf에서 compiler-estimated FLOP를 사용했다면 `--flop-source compiler_estimate`로 출처를 남긴다. VMEM을 포함한 전체 메모리 접근과 HBM 트래픽은 다르다. 엔드투엔드 서버 로그, 토큰 수, KV 점유율만으로 커널 FLOP와 실제 HBM 전송량은 결정할 수 없다. 타이밍 실험과 프로파일 캡처는 별도 실행으로 구분한다. [XProf Roofline](https://openxla.org/xprof/roofline_model) · [TPU 프로파일 캡처](https://docs.cloud.google.com/tpu/docs/profile-tpu-vm) · [Nsight Compute CLI](https://docs.nvidia.com/nsight-compute/NsightComputeCli/)

프로파일을 연산당 1행인 CSV로 내보내고 실제 열 이름을 지정한다. 아래 숫자는 단위 검증용 가상 예시다. 출력 CSV의 `peak_tflops, hbm_tb_s, ops_gflop, hbm_gb, time_ms` 다섯 값은 HTML의 **커널 Roofline** 입력 순서와 같다. 여러 칩·여러 호출이 겹치는 집계에서는 wall time과 합산 kernel time의 범위를 먼저 맞춘다.

```bash
python3 analyze_logs.py --out results/roof.csv roof examples/profile.csv \
  --run-id demo-001 --phase decode --scope one-chip --flop-source synthetic \
  --name operation --time time_us --time-unit us --flops total_flops --bytes total_hbm_bytes \
  --peak 200 --bandwidth 2
```

## 파일 구성과 검증

| 경로 | 역할 |
|---|---|
| `web/index.html` | 오프라인 분석 화면. 원격 수집 탭에서 명령 생성 |
| `remote.py` | PC용 실행 계획, Pod·VM 접속, 결과 회수, 재수거, 기존 JSON 변환과 CSV 병합 |
| `worker.py` | 원격 환경 기록, 서버 기동, 샘플링, 부하 생성, 상태 판정 |
| `analyze_logs.py` | 메트릭 시계열 요약과 프로파일 CSV의 단위 변환 |
| `configs/targets.example.json`, `examples/` | 버전 관리 가능한 설정 양식과 장비 없는 연습 자료 |
| `docs/data-contract.md`, `tests/` | 필드 사전과 모의 전송·기동·수거 회귀 테스트 |

프로젝트 루트에서 `python3 -m unittest discover -s tests -v`를 실행한다. `tests/test_remote.py`는 Linux/macOS에서 동작하는 모의 SSH·kubectl·vLLM 실행 파일을 사용한다. Git에는 예시 설정과 코드만 추가하고 실제 접속 설정, 모델, 원시 로그, 결과는 제외한다. 실장비용 vLLM 설치·버전은 플랫폼에서 이미 검증한 CUDA/TPU 이미지 또는 venv를 그대로 사용한다. [vLLM 서버 옵션](https://docs.vllm.ai/en/stable/cli/serve/) · [vLLM 벤치마크 옵션](https://docs.vllm.ai/en/stable/cli/bench/serve/) · [kubectl exec](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_exec/)
