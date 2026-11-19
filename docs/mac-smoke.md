# 맥북에서 실제 모델로 수집기 점검하기

저장한 GGUF 모델로 텍스트를 생성하고 **실제 응답 → 지연·처리량 → LogKit JSON → CSV → 대시보드**가 이어지는지 확인한다. 이 경로는 llama.cpp CPU 4스레드를 사용한다. 운영 환경의 vLLM 엔진 지표, Pod 연결, GPU·TPU 커널 Roofline은 해당 장비에서 별도로 검증한다.

## 실행

1. Python 3.9 이상, GGUF 텍스트 생성 모델, `llama-server`를 준비한다. 스크립트는 PATH에 있는 `llama-server`를 찾고, 없으면 이 맥북에서 확인한 Atomic Chat 내장 실행 파일을 사용한다.
2. 프로젝트 폴더에서 아래 명령 중 하나를 실행한다. 첫 명령은 이 맥북에 이미 있는 Ollama 모델 파일을 읽는다. Ollama 서비스를 시작하거나 모델을 새로 다운로드할 필요가 없다.
3. 출력 마지막 줄의 `results/local-smoke-작업ID/runs.csv`를 `web/index.html`의 **서빙 분석 → 측정 파일 열기**에서 연다. 기본값인 품질 미확인 상태에서도 **설정별 측정 결과**에 실측값이 표시된다.

```bash
python3 mac_smoke.py
python3 mac_smoke.py --gguf /path/to/model.gguf --server-bin /path/to/llama-server
```

기본값은 워밍업 1건, 순차 측정 5건, 요청당 출력 12토큰이다. 서버는 PC의 빈 루프백 포트를 할당받는다. 성공·오류·Ctrl+C 시 이번 실행에서 시작한 모델 서버를 종료한다.

## 이 맥북에서 얻은 결과

Apple M5 Max, 통합 메모리 36GB, llama.cpp 빌드 `b11734-f3fb2fcdb`, `gemma4:e4b`의 텍스트 가중치(`Q4_K - Medium`)로 실행했다. 모델 입력은 동일한 영어 프롬프트 11토큰이다. 초기 서버 기동과 워밍업은 집계에서 제외했다.

| 항목 | 실측 |
|---|---:|
| 완료 / 실패 | 5 / 0건 |
| 총 출력 / 측정 구간 | 60토큰 / 3.173초 |
| 전체 구간 출력 처리량 | 18.91토큰/초 |
| p95 첫 텍스트 응답 | 286.8ms |
| p95 출력 청크 간격 | 33.6ms |

p95는 관측값을 정렬해 95% 지점의 값을 선택한다. 요청 5건에서 TTFT p95는 가장 느린 요청이다. 출력 간격은 첫 청크를 제외한 55개 간격으로 계산했다. 측정 요청마다 텍스트 청크 12개와 서버의 출력 토큰 집계 12개가 일치해야 CSV를 만든다. 지연은 클라이언트 수신 시각 기준이다. 이 짧은 점검은 한 설정의 동작 확인용이다.

## 기록을 다시 확인하기

| 파일 | 내용 |
|---|---|
| `manifest.json`, `server.log` | 모델 지문, 백엔드 빌드, 기동 인자, 프롬프트, 서버 로그 |
| `warmup.json`, `requests.json` | 워밍업과 본 측정의 생성 텍스트, 토큰 수, 첫 응답 시간과 청크 사이 간격 |
| `bench.json`, `run.json` | 기존 변환기에 넘긴 측정값과 환경 정보. 모델의 응답 품질은 `unknown` |
| `html.json`, `runs.csv` | 기존 `remote.py extract`와 `merge`가 만든 대시보드 입력 |

공통 CSV에서 `max_num_seqs`, `max_num_batched_tokens`, `max_model_len` 열에는 이번에 실행한 llama.cpp의 `--parallel 1`, `--batch-size 64`, `--ctx-size 512`를 순서대로 담는다. 대시보드는 로컬 환경을 선택하면 해당 옵션 이름을 표시한다. 이 대응은 단일 슬롯에서의 설정 기록용이다. 두 엔진의 스케줄링 동작과 성능을 동일시할 수 없다. [llama.cpp 서버 옵션](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
