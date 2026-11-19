# 수집 시점과 데이터 계약

한 번의 측정은 하나의 `run_id`로 식별한다. 대상 ID는 접속 위치, 설정 ID는 vLLM 서버 상한 조합, 시나리오 ID는 클라이언트 부하를 뜻한다. 시나리오와 환경에 붙는 해시는 사람이 같은 이름을 쓰더라도 기록된 조건이 달라진 실험을 분리한다.

| 시점 | 입력·관측 데이터 | 출처 / 파일 | 목적 |
|---|---|---|---|
| 접속 전 | context, namespace, pod, container 또는 SSH 별칭, 원격 python, url, 선택적인 tmp | targets.json, receipt.json | 어느 Pod/VM의 어느 프로세스를 관측했는지 확인 |
| 접속 시 | Pod UID, 노드, imageID, containerID, restartCount | kubectl get pod → receipt.json | 회수 전후 대상이 달라지면 정상 수거를 중단 |
| 실험 준비 | model_id, revision, model_path, served_name, tokenizer_path, tokenizer_revision, dtype, quantization | 배포자가 기입 → manifest.json | 모델과 토크나이저 식별. revision은 로컬 가중치와 일치하는 커밋 또는 사내 아티팩트 ID로 기입 |
| 실험 준비 | hardware, devices, backend, server_build, tp, dp, fixed_options, server_args | 배포 명세 → manifest.json | 칩 종류·수, 병렬화, 소프트웨어 및 고정 옵션 확인 |
| 실행 환경 | Python 경로, vllm/torch/libtpu/플러그인 등 설치 버전, CUDA_VISIBLE_DEVICES, PJRT_DEVICE, TPU_ACCELERATOR_TYPE, VLLM_USE_V1 등 허용 목록 | 원격 수집기 프로세스 → host.json | 어떤 인터프리터에서 버전을 읽었는지 확인. nvidia-smi/tpu-info가 있으면 명령 상태와 출력 추가 |
| 기존 모델 | 모델 별칭, 서버가 공개한 max_model_len, 선택적 server_pid의 명시적 CLI·환경 | /v1/models → ready.json, /proc → service-process.json | 기존 서버의 설정은 운영 명세에서 확인. 키·토큰과 전체 환경 목록은 수집 대상에서 제외 |
| 신규 기동 | argv, 기동 UTC, ready UTC, launch_to_ready_s, 표준 출력·오류 | launch.json, ready.json, server.log | 모델 로딩·초기화·컴파일·KV 할당 단계 확인. ready까지 시간은 여러 단계 합계 |
| 부하 시작 전 | input_tokens, output_tokens, concurrency, num_prompts, warmup_prompts, seed, repeats, request-rate=inf, ignore-eos | run.json, manifest.json, warmup-command.json | 토큰 길이·분포와 클라이언트 동시성 통제. 워밍업은 별도 프로세스로 실행 |
| 부하 처리 중 | UTC, 라벨 포함 vllm:* 전체 샘플, scrape 성공/실패 | /metrics → metrics.jsonl.gz, scrapes.json | 실제 시계열 보존. 기본 주기 2초, 요청·수집 시간만큼 지연 가능. 짧은 스파이크는 생략될 수 있음 |
| 부하 종료 | completed, failed 또는 계획 개수, p95_ttft_ms, p95_itl_ms, output_throughput, request_throughput, 노출된 원시 통계 | bench.json, bench.log, bench-command.json | 집계 TTFT·ITL 단위 ms. output_throughput은 출력 토큰/초, request_throughput은 완료 요청/초 |
| 실패 | 오류 단계·원인, 해당 실행 로그에서 OOM 단서, 기동 실패 | error.json, setting-error.json, worker.log, server.log | 실패한 실행의 수치 칸을 비운다. 기동부터 실패한 설정은 미측정으로 남긴다 |
| 별도 평가·프로파일 | quality와 평가 근거, 연산명·phase·칩 범위·정밀도·FLOP 산정 방식·HBM traffic·시간 | 품질 평가 원본, Nsight/XProf 내보내기 | 정합성과 하드웨어 Roofline을 별도 근거로 연결 |

## kubectl로 직접 관측한 데이터

`kube`는 실행마다 `results/kube-작업ID/`를 만든다. HTML 입력은 `kubectl-summary.json`이며, 이 파일을 열면 아래 정보를 확인할 수 있다.

| 항목 | 기록 내용 |
|---|---|
| 대상과 시각 | context·namespace·pod·container·HTTP 포트, 관측 시작·종료 UTC, 요청한 관측 초와 수집 간격 |
| 대상 일치 여부 | 시작·종료 시 Pod UID·컨테이너 ID·이미지·재시작 횟수. 교체됐거나 마지막 신원을 확인할 수 없으면 요약값을 제외 |
| 메트릭 | 표본별 원본은 `metrics.jsonl.gz`, 성공·실패 횟수는 `scrapes.json`. 요약은 이름·라벨별 처음/최대/마지막 값, 표본 수, 카운터 증가량과 리셋 횟수 |
| 로그 | 선택한 컨테이너에서 조회한 줄 수, 관련 설정·기동·메모리 문구의 줄 수, OOM 문구의 줄 수. 원문은 `pod.log`, 관련 줄은 `events.log` |
| 수집 상태 | `ok` 정상, `partial` 부분 성공, `invalid` 분석 제외. 일부 소스가 빠지면 `warnings`에 사유를 기록. 로그 전용은 `logs_only=true` |

## 엔진 메트릭

| 키 예시 | 단위·의미 | 해석 |
|---|---|---|
| vllm:num_requests_running / num_requests_waiting | 실행·대기 요청 개수 | 같은 부하에서 큐와 TTFT가 함께 늘었는지 확인한다. 클라이언트 concurrency는 실제 실행 배치와 다르다. |
| vllm:kv_cache_usage_perc | 사용된 KV 블록 비율 0~1 | 0.80은 예약한 캐시 중 약 80%를 뜻한다. HBM 전체의 80%라는 의미로 사용할 수 없다. |
| vllm:num_preemptions_total | 누적 선점 횟수 | 첫·끝 샘플 사이 증가량을 계산한다. 리셋 시 해당 구간을 표시한다. |
| vllm:prompt_tokens_total / generation_tokens_total | 누적 입력·생성 토큰 수 | 샘플 구간의 서버 처리량 추세에 사용한다. 동일 replica의 다른 트래픽도 포함될 수 있다. |
| vllm:prefix_cache_queries_total / hits_total | 누적 prefix cache 조회·적중 단위 | 버전에 따른 단위·라벨을 확인하고 같은 라벨의 증가량끼리 계산한다. |
| vllm:request_queue_time_seconds_* / time_to_first_token_seconds_* / inter_token_latency_seconds_* | 서버측 히스토그램, 초 | raw 샘플에 보존한다. 클라이언트 p95와 측정 시작점·집계 방식이 다르다. |

엔진과 모델 라벨은 보존한다. `analyze_logs.py metrics`는 게이지의 관측 최솟값·최댓값, 카운터의 관측 증가량·리셋 수를 계산한다. 표본이 없는 구간은 0으로 보정하지 않는다. scrape 구간에는 CLI 시작 비용과 종료 비용이 일부 포함되므로 처리량 비교에는 bench JSON을 사용한다. [vLLM Production Metrics](https://docs.vllm.ai/en/latest/usage/metrics/) · [vLLM 메트릭 설계](https://docs.vllm.ai/en/latest/design/metrics/)

## HTML 필수 열

| 열 | 규칙 |
|---|---|
| run_id, scenario, model, dtype, environment | 실행별 고유 ID, 시나리오·워크로드 해시, 모델@리비전, 실행 dtype, 장비·빌드·고정 옵션 식별자 |
| input_tokens, output_tokens, concurrency | 계획한 요청당 입력·출력 길이와 동시 요청 상한. 출력 길이는 ITL 측정을 위해 2 이상. |
| max_num_seqs, max_num_batched_tokens, max_model_len | 스텝당 시퀀스 수 상한, 스텝의 전체 토큰 예산, 요청당 문맥 상한. 세 값은 양의 정수. |
| status, quality, data_kind | ok/error/oom, pass/fail/unknown, measured/demo. 새로 수집한 quality는 unknown. |
| p95_ttft_ms, p95_itl_ms | 각 요청의 첫 토큰 지연과 스트리밍 출력 간 지연의 95번째 백분위수. TPOT나 p99로 대체하지 않는다. |
| output_throughput, request_throughput | 초당 출력 토큰 수·완료 요청 수. 요청 처리량은 원본에 없는 경우 비운다. |

각 실행 폴더의 `html.json`과 PC의 `runs.csv`가 대시보드 입력이다. 품질 평가를 완료하면 해당 실행의 `run.json` 사본에 `quality=pass/fail`을 기록하고 원래의 bench.json과 함께 extract한다. 동일 run_id에 값이 다른 두 행은 병합 오류로 표시하므로 분석에는 갱신한 한 행만 포함한다. HTML은 같은 조건에서 반복별 처리량 중앙값과 반복별 p95 중 최댓값을 사용한다.

## 데이터 보존 범위

원시 `server.log`, `pod.log`, systemd 저널은 대상 서비스 자체가 출력한 텍스트다. 운영 서비스가 요청 본문 등을 로깅하는 경우 원시 로그에도 포함될 수 있다. Git에 넣는 기본 파일은 예시 설정, 수집기, 분석 화면, 테스트다. 실측 데이터는 별도 결과 디렉터리에 보관한다.
