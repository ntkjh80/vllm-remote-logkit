# 실측용 모델을 띄우고 첫 결과 얻기

**맥북에서 수집 과정을 확인하려면 1번**, **H200 VM이나 Ironwood Pod의 성능을 측정하려면 2~4번**을 따른다. 명령의 실행 위치를 각 단계에 표시했다.

| 목적 | 실행 방식 | 측정이 끝나면 |
|---|---|---|
| 이 맥북에서 한 번 재현 | `mac_smoke.py`가 로컬 GGUF 모델 기동 | 모델 종료, 맥북에 CSV 저장 |
| 빈 전용 VM·Pod에서 실측 | 맥북에서 `remote.py launch` 실행. 원격 수집기가 vLLM 기동 | 이번에 띄운 모델 종료, 맥북에 CSV 회수 |
| 직접 띄운 서버를 계속 사용 | 서버에서 `vllm serve`, 맥북에서 `remote.py collect` | 서버는 계속 실행, 맥북에 CSV 회수 |
| 서비스 중인 Pod의 상태만 확인 | 맥북에서 `remote.py kube` 실행 | 추가 추론 요청 없이 상태 요약 JSON 저장 |

## 1. 맥북 한 대로 먼저 실행

**맥북의 터미널**에서 압축을 푼 상위 폴더로 이동한 뒤 실행한다. 아래 예시는 로컬에 설치한 `gemma4:e4b` 가중치와 `llama-server`를 사용한다. Python 3.9 이상이 필요하다. Ollama 데몬을 별도로 켤 필요는 없다.

```bash
cd vllm-remote-logkit
python3 mac_smoke.py
open web/index.html
```

1. 스크립트가 빈 로컬 포트에서 CPU 4스레드로 모델을 띄운다. 첫 워밍업 요청을 제외하고 5건을 순서대로 보낸다.
2. 터미널에 `5건 완료`와 `results/local-smoke-…/runs.csv` 경로가 나오면 모델 구동과 수집이 끝난 것이다. 대시보드 **서빙 분석 → 측정 파일 열기**에서 그 CSV를 선택한다.
3. 생성 문장 자체는 같은 폴더의 `requests.json`에서 확인한다. 대시보드의 품질 상태는 별도 평가 전까지 `미확인`이다.

다른 맥에서 재현할 때는 로컬 GGUF 텍스트 생성 모델과 해당 모델을 지원하는 llama.cpp 서버를 준비한다. 예를 들어 `python3 mac_smoke.py --gguf /path/to/model.gguf --server-bin /path/to/llama-server`로 경로를 직접 지정한다. 이 점검은 영문 고정 프롬프트에서 12개 출력 토큰과 12개 텍스트 청크가 일치하는지 확인한다. [맥북 실측값과 로그 읽기](mac-smoke.md)

## 2. 원격 장비에 런타임과 모델 준비

아래의 실측 예시는 **Qwen3-8B 텍스트 모델, BF16, API 이름 `bench-qwen3-8b`**를 사용한다. 먼저 장비 하나에서 연결을 확인한 뒤 두 장비를 비교한다. 맥북에서 쓴 양자화 GGUF와 원격 BF16 모델은 서로 다른 실험이다. [사용할 모델 리비전](https://huggingface.co/Qwen/Qwen3-8B/tree/b968826d9c46dd6066d109eabc6255188de91218)

### H200 VM

**맥북**에서 `ssh h200-bench`로 접속한다. `h200-bench`는 예시 SSH 별칭이다. **VM**에서 아래 값을 확인한다. 기존 실험용 vLLM 환경이 있으면 해당 환경을 활성화하고 사용한다.

```bash
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
command -v python
command -v vllm
vllm --version
vllm bench serve --help
```

전용 VM에 vLLM이 없다면, 호환 NVIDIA 드라이버와 `uv`가 설치된 상태에서 아래처럼 별도 환경을 만든다. 아래 설치는 최초 점검용이다. 본 실험에서는 검증한 vLLM 버전과 드라이버를 기록하고 같은 조합을 유지한다.

```bash
uv venv --python 3.12 --seed "$HOME/.venvs/logkit-vllm"
source "$HOME/.venvs/logkit-vllm/bin/activate"
uv pip install 'vllm[bench]' --torch-backend=auto
vllm --version
command -v python
command -v vllm
```

마지막 두 경로를 3번 설정의 `connection.python`, `vllm_bin`에 각각 넣는다. 원격 수집기는 비대화식 SSH로 시작하므로 절대경로를 쓰는 편이 확실하다. 설치 릴리스가 사전 커널 다운로드를 요구하면 해당 릴리스의 Quickstart를 따른다. [vLLM 설치](https://docs.vllm.ai/en/stable/getting_started/quickstart/) · [벤치마크 패키지](https://docs.vllm.ai/en/stable/cli/index.html)

### Ironwood GKE

**맥북**에서 실제 context, 네임스페이스, Pod, 모델 컨테이너를 확인한다. 아래 `YOUR_CONTEXT`, `YOUR_POD`, `inference`, `vllm`을 환경에 맞게 바꾼다.

```bash
kubectl config get-contexts
kubectl --context YOUR_CONTEXT -n inference get pods -o wide
kubectl --context YOUR_CONTEXT -n inference get pod YOUR_POD \
  -o jsonpath='{range .spec.containers[*]}{.name}{"  "}{.resources.limits}{"\n"}{end}'
kubectl --context YOUR_CONTEXT -n inference exec -it YOUR_POD -c vllm -- sh
```

**컨테이너 안**에서 `command -v python3`, `command -v vllm`, `vllm --version`, `vllm bench serve --help`를 확인한다. TPU용 vLLM과 플러그인이 설치된 이미지를 사용한다. 이미지가 JAX 백엔드인지 TorchTPU인지, 이미지 digest와 실제 TP 값을 기록한다. 새 Pod가 필요하면 팀의 GKE 배포 절차로 Ironwood 리소스·토폴로지·모델 볼륨을 먼저 할당한다. LogKit의 `launch`는 준비된 컨테이너 안에서 서버 프로세스를 시작한다. [GKE TPU 할당](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/tpus) · [vLLM JAX 기동](https://docs.vllm.ai/projects/vllm-jax/en/latest/getting_started/quickstart/) · [TorchTPU 기동](https://docs.vllm.ai/projects/vllm-torchtpu/en/latest/getting_started/quickstart/)

설정 예시의 TPU는 **단일 호스트, 물리 칩 4개를 할당하고 TP=4를 사용하도록 구성한 경우**다. `devices`에는 물리 칩 수, `tp`에는 백엔드에서 실제 사용하는 텐서 병렬화 크기를 각각 입력한다. 두 값은 별도 항목이다. 여러 호스트가 한 모델을 서빙하는 슬라이스는 해당 백엔드의 분산 배포 절차로 먼저 띄운 뒤, API를 제공하는 Pod에 `collect`를 연결한다.

### GPU·TPU에 같은 가중치 놓기

**가중치를 저장할 Linux VM 또는 모델 볼륨에 쓰기 가능한 환경**에서 실행한다. 예시는 쓰기 가능한 `/models`와 Hugging Face CLI(`hf`)를 전제로 한다. 읽기 전용 Pod에서는 준비한 볼륨을 마운트한다. 두 장비가 같은 볼륨을 쓸 수 있으면 한 번만 받는다. 내부 미러를 쓰면 같은 리비전의 전체 스냅샷을 복사한다.

```bash
BENCH_REV=b968826d9c46dd6066d109eabc6255188de91218
BENCH_MODEL_DIR=/models/Qwen3-8B-b968826
hf download Qwen/Qwen3-8B --revision "$BENCH_REV" --local-dir "$BENCH_MODEL_DIR"
ls "$BENCH_MODEL_DIR"/config.json "$BENCH_MODEL_DIR"/tokenizer.json "$BENCH_MODEL_DIR"/*.safetensors
```

`model_path`와 `tokenizer_path`에는 **모델 프로세스가 실제로 보는 절대경로**를 넣는다. 위 예시는 가중치와 토크나이저를 같은 리비전에서 읽는다. [Hugging Face CLI](https://huggingface.co/docs/huggingface_hub/guides/cli)

## 3. 맥북에서 모델 기동과 첫 측정을 한 번에

프로젝트 폴더에서 설정을 복사한다. `cp -n`은 이미 작성한 개인 설정을 보존한다. 아래 모든 명령은 이 파일을 `--config`로 명시한다.

```bash
cp -n configs/qwen3-8b.example.json configs/qwen3-8b.json
```

`configs/qwen3-8b.json`을 열고 **측정하려는 대상**의 다음 값을 채운다. 모델 리비전, API 이름, 부하 조건은 예시에 들어 있다.

| 항목 | H200 VM | Ironwood Pod |
|---|---|---|
| 연결 | `connection.host`에 실제 SSH 별칭 | 실제 `context`, `namespace`, `pod`, `container` |
| 실행 파일 | `connection.python`, `vllm_bin`에 VM 안의 절대경로 | 컨테이너 안의 Python과 vLLM 경로. PATH가 다르면 `vllm_bin`을 추가 |
| 하드웨어 | 예시는 사용 GPU 1개, TP=1 | 실제 `devices`·`tp`·`dp`·`backend` 입력 |
| 실험 식별 | `server_build`에 vLLM 버전·빌드, `fixed_options`에 고정한 캐시·컴파일 조건 | 이미지 digest와 플러그인 버전, 캐시·컴파일 조건 기록 |
| 모델 위치 | VM의 `model_path`·`tokenizer_path` | Pod에 마운트한 경로 |

**빈 전용 VM으로 시작하는 예시**다. 8000번 포트가 비어 있고 선택한 가속기를 다른 서버가 사용하지 않는 환경에서 실행한다. 첫 줄은 실행 범위 확인, 두 번째 줄이 실제 기동이다.

```bash
python3 remote.py launch --config configs/qwen3-8b.json --target h200-vm --setting baseline --scenario first-run --plan
python3 remote.py launch --config configs/qwen3-8b.json --target h200-vm --setting baseline --scenario first-run
```

전용 TPU Pod에서는 같은 명령에서 `--target h200-vm`을 `--target ironwood-pod`로 바꾼다. `--plan`은 원격 접속과 가중치 로딩을 수행하지 않는다.

| 실측에서 확인할 것 | 첫 실행의 기대값 |
|---|---|
| 서버가 준비됐나? | 회수된 `baseline/ready.json`의 모델 목록에 `bench-qwen3-8b`가 표시됨 |
| 모델이 추론했나? | `baseline/<실행ID>/bench.json`의 `completed=8`, 실패 0건. 워밍업 2건은 집계에서 제외 |
| 결과는 어디에 있나? | 맥북의 `results/<대상ID>-<작업ID>/runs.csv`. HTML **서빙 분석 → 측정 파일 열기**에서 선택 |
| 실패했다면? | 기동 실패는 `baseline/server.log`·`setting-error.json`, 측정 실패는 실행 폴더의 `error.json`·`bench.log`부터 확인 |

첫 실행은 동시 요청 1건, 요청당 입력 64토큰·출력 16토큰이다. 기동과 수집을 확인한 뒤, 아래 명령으로 **배치 토큰 예산만 2,048 → 4,096**으로 바꿔 비교한다. `compare`는 입력 512·출력 128토큰, 동시 요청 1·8건, 조건별 2회 반복이다. 한 대상당 2설정 × 2부하 × 2반복으로 CSV 8행을 기대한다. `quality=unknown`은 별도 응답 품질 평가가 남았다는 뜻이다.

```bash
python3 remote.py launch --config configs/qwen3-8b.json --target h200-vm --scenario compare
```

## 4. 직접 모델을 켜 두고 측정하는 방법

이미 서버가 떠 있으면 아래 기동 명령은 건너뛰고 상태 확인부터 한다. 새 서버를 직접 띄울 때는 **H200 VM 터미널** 또는 **전용 TPU Pod의 셸**에서 실행한다. 아래는 H200 1GPU 예시다. TPU에서는 `BENCH_TP`를 검증한 값으로 바꾸고 이미지에 필요한 환경을 적용한다. 모델 파일 준비는 2번과 같다.

```bash
BENCH_TP=1
BENCH_MODEL_DIR=/models/Qwen3-8B-b968826
vllm serve "$BENCH_MODEL_DIR" --tokenizer "$BENCH_MODEL_DIR" \
  --served-model-name bench-qwen3-8b --host 127.0.0.1 --port 8000 \
  --dtype bfloat16 --tensor-parallel-size "$BENCH_TP" --data-parallel-size 1 \
  --max-model-len 2048 --max-num-seqs 16 --max-num-batched-tokens 2048
```

터미널을 열어 둔다. **맥북의 두 번째 터미널**에서 대상에 맞는 터널 하나를 실행한다. VM은 SSH, Pod는 kubectl을 선택한다.

```bash
# H200 VM
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18000:127.0.0.1:8000 h200-bench
```

```bash
# Ironwood Pod
kubectl --context YOUR_CONTEXT -n inference port-forward --address 127.0.0.1 pod/YOUR_POD 18000:8000
```

**맥북의 세 번째 터미널**에서 실제 응답을 확인한다. 아래는 API 키가 없는 전용 테스트 서버 예시다. 키가 필요한 서비스는 해당 서비스용 인증 헤더를 사용한다.

```bash
curl -fsS http://127.0.0.1:18000/health
curl -fsS http://127.0.0.1:18000/v1/models
curl -fsS http://127.0.0.1:18000/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"bench-qwen3-8b","prompt":"The capital of France is","max_tokens":8,"temperature":0}'
```

`/health`가 HTTP 200, `/v1/models`의 `id`가 설정의 `served_name`과 일치하고 추론 응답의 `choices[0].text`와 `usage.completion_tokens`가 채워지면 첫 요청까지 성공한 것이다. 이 수동 요청은 벤치마크 집계에서 제외한다.

같은 터미널에서 프로젝트 폴더로 이동해 아래 명령을 실행한다. `collect`는 기존 서버에 부하를 보낸다. 실제 기동한 옵션과 일치하는 `--setting`을 지정한다. 수집기는 VM·Pod 안에서 `127.0.0.1:8000`을 사용하므로 설정 파일의 포트는 그대로 둔다. 위 터널은 사람이 응답을 확인하는 용도다.

```bash
python3 remote.py collect --config configs/qwen3-8b.json --target h200-vm --setting baseline --scenario first-run
```

Pod는 대상을 `ironwood-pod`로 바꾼다. Kubernetes에서 기존 트래픽만 보려면 `python3 remote.py kube --context YOUR_CONTEXT -n inference --pod YOUR_POD -c vllm --seconds 30`을 사용한다. 수동으로 띄운 실험 서버와 터널은 각각 실행한 터미널에서 Ctrl+C로 종료한다. [Pod 관측 파일 읽기](kubectl-quickstart.md) · [vLLM 기동 옵션](https://docs.vllm.ai/en/stable/cli/serve/)
