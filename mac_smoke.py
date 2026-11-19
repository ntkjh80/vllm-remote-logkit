#!/usr/bin/env python3
"""Mac의 GGUF 텍스트 모델로 실제 요청부터 기존 LogKit CSV 생성까지 점검한다."""
import argparse
import hashlib
import http.client
import json
import math
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from worker import child, save, utc

ROOT = Path(__file__).resolve().parent
ATOMIC_SERVER = Path("/Applications/Atomic Chat.app/Contents/Resources/resources/llamacpp-backend-upstream/build/bin/llama-server")
PROMPT = "The numbers from one to five are: one,"
CTX, BATCH, SLOTS, LIMIT = 512, 64, 1, 12


def resolve_model(args):
    if args.gguf:
        path = args.gguf.expanduser().resolve()
        if not path.is_file():
            raise ValueError("GGUF 파일이 없습니다: " + str(path))
        # 모델 경로가 달라도 같은 파일의 측정을 식별한다.
        with path.open("rb") as f:
            h = hashlib.sha256()
            for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
                h.update(block)
        return path, path.name, h.hexdigest()
    name, sep, tag = args.ollama_model.partition(":")
    if not re.fullmatch(r"[\w.-]+", name) or (sep and not re.fullmatch(r"[\w.-]+", tag)) or name in (".", "..") or tag in (".", ".."):
        raise ValueError("로컬 Ollama 라이브러리의 모델명:태그 또는 --gguf 경로를 입력하세요.")
    tag = tag if sep else "latest"
    store = Path(os.environ.get("OLLAMA_MODELS") or Path.home()/".ollama/models")
    manifest = store/"manifests"/"registry.ollama.ai"/"library"/name/tag
    if not manifest.is_file():
        raise ValueError(f"설치된 {name}:{tag}를 찾을 수 없습니다. 다른 --ollama-model 또는 --gguf 파일을 지정하세요.")
    for layer in json.loads(manifest.read_bytes())["layers"]:
        if layer.get("mediaType") == "application/vnd.ollama.image.model":
            digest = layer["digest"]
            if re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                path = store/"blobs"/digest.replace(":", "-")
                if path.is_file() and path.stat().st_size == layer["size"]:
                    return path, name + ":" + tag, digest[7:]
    raise ValueError("Ollama 모델의 GGUF 가중치 파일을 확인하세요.")


def get_json(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        if resp.status != 200:
            raise OSError(f"{path} HTTP {resp.status}")
        return json.load(resp)
    finally:
        conn.close()


def ready(proc, port, alias):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("서버 기동 실패. server.log를 확인하세요.")
        try:
            models = get_json(port, "/v1/models")["data"]
            model = next((m for m in models if m.get("id") == alias), None)
            if model and get_json(port, "/health").get("status") == "ok":
                return model, get_json(port, "/props")
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(.25)
    raise RuntimeError("서버 기동 120초 초과. server.log를 확인하세요.")


def infer(port, alias):
    payload = dict(model=alias, prompt=PROMPT, max_tokens=LIMIT, seed=7, temperature=0,
                   ignore_eos=True, cache_prompt=False, stream=True, stream_options={"include_usage": True})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    fragments, times, usage, reason, ended = [], [], {}, None, False
    begin = time.perf_counter()
    try:
        conn.request("POST", "/v1/completions", json.dumps(payload), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        if resp.status != 200:
            raise RuntimeError(f"추론 HTTP {resp.status}: {resp.read(300).decode(errors='replace')}")
        for line in resp:
            if not line.startswith(b"data:"):
                continue
            received = time.perf_counter()
            data = line[5:].strip()
            if data == b"[DONE]":
                ended = True
                break
            event = json.loads(data)
            if "error" in event:
                raise RuntimeError(str(event["error"]))
            usage = event.get("usage") or usage
            for choice in event.get("choices", []):
                if choice.get("text"):
                    times.append(received)
                    fragments.append(choice["text"])
                reason = choice.get("finish_reason") or reason
    finally:
        conn.close()
    # 청크가 여러 토큰을 묶었을 때 청크 간격을 토큰 간격으로 사용하지 않는다.
    if not ended or reason != "length" or len(times) != LIMIT or usage.get("completion_tokens") != LIMIT:
        raise ValueError("스트림 종료·토큰 수·텍스트 청크 수가 12토큰 점검 조건에 맞지 않습니다.")
    return {"input_tokens": usage["prompt_tokens"], "output_tokens": usage["completion_tokens"],
            "cached_input_tokens": usage.get("prompt_tokens_details", {}).get("cached_tokens"),
            "ttft_ms": 1000 * (times[0]-begin), "itl_ms": [1000*(b-a) for a,b in zip(times,times[1:])],
            "text": "".join(fragments), "finish_reason": reason}


def p95(values):
    """표본의 95% 이상을 포함하는 가장 작은 관측값(nearest rank)."""
    return sorted(values)[math.ceil(.95*len(values))-1]


def run(args):
    path, model, revision = resolve_model(args)
    exe = args.server_bin or shutil.which("llama-server") or ATOMIC_SERVER
    if not os.access(exe, os.X_OK):
        raise ValueError("llama-server를 찾을 수 없습니다. --server-bin에 실행 파일을 지정하세요.")
    with path.open("rb") as f:
        if f.read(4) != b"GGUF":
            raise ValueError("GGUF 텍스트 생성 모델을 지정하세요.")
    if not 1 <= args.requests <= 100:
        raise ValueError("--requests 범위는 1~100입니다.")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    rid = uuid.uuid4().hex[:12]
    alias = "local-smoke-" + rid
    dest = args.out / alias
    dest.mkdir(parents=True)
    flags = ["--device", "none", "--n-gpu-layers", "0", "--no-kv-offload", "--no-op-offload",
             "--no-mmproj", "--no-repack", "--no-warmup", "--ctx-size", str(CTX), "--batch-size", str(BATCH),
             "--ubatch-size", str(BATCH), "--parallel", str(SLOTS), "--threads", "4"]
    cmd = [str(exe), "--model", str(path), "--alias", alias, "--host", "127.0.0.1", "--port", str(port), *flags]
    print(f"{model} · CPU 4스레드 · 결과 {dest}", flush=True)
    with child(cmd, dest/"server.log") as proc:
        info, props = ready(proc, port, alias)
        if props["total_slots"] != SLOTS or props["default_generation_settings"]["n_ctx"] != CTX:
            raise ValueError("실제 서버의 슬롯 또는 문맥 상한이 기동 설정과 다릅니다.")
        version, dtype = props["build_info"], info["meta"]["ftype"]
        save(dest/"manifest.json", {"start_utc":utc(),"backend":"llama.cpp CPU","build":version,
             "model":model,"weights_sha256":revision,"weight_format":dtype,"server_flags":flags,
             "prompt":PROMPT,"ignore_eos":True,"cache_prompt":False,"seed":7,"temperature":0,
             "warmup_requests":1,"measured_requests":args.requests,"max_output_tokens":LIMIT})
        save(dest/"warmup.json", infer(port, alias))
        begin = time.perf_counter()
        rows = [infer(port, alias) for _ in range(args.requests)]
        elapsed = time.perf_counter()-begin
    save(dest/"requests.json", rows)
    if len({r["input_tokens"] for r in rows}) != 1:
        raise ValueError("고정 프롬프트의 입력 토큰 수가 요청마다 다릅니다.")
    raw = dict(completed=len(rows), failed=0, num_prompts=args.requests, max_concurrency=1,
               p95_ttft_ms=p95([r["ttft_ms"] for r in rows]), p95_itl_ms=p95([v for r in rows for v in r["itl_ms"]]),
               output_throughput=sum(r["output_tokens"] for r in rows)/elapsed, request_throughput=len(rows)/elapsed,
               measured_seconds=elapsed)
    chip = platform.machine()
    if sys.platform == "darwin":
        info = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True)
        chip = info.stdout.strip() or chip
    meta = dict(run_id=rid, scenario="mac-smoke-fixed-12-n"+str(args.requests), model=model+"@"+revision[:12],
                dtype=dtype, environment=f"llama.cpp local-smoke · {chip} CPU 4T · {version}",
                input_tokens=rows[0]["input_tokens"], output_tokens=LIMIT, concurrency=1,
                max_num_seqs=SLOTS, max_num_batched_tokens=BATCH, max_model_len=CTX, quality="unknown",
                data_kind="measured", num_prompts=args.requests)
    save(dest/"bench.json", raw)
    save(dest/"run.json", meta)
    # 원격 수집에서 사용하는 실제 CLI 두 개를 그대로 실행한다.
    cli = [sys.executable, str(ROOT/"remote.py")]
    subprocess.run(cli+["extract","--raw",str(dest/"bench.json"),"--meta",str(dest/"run.json"),"--out",str(dest/"html.json")],check=True)
    subprocess.run(cli+["merge",str(dest),"--out",str(dest/"runs.csv")],check=True)
    print(f'{len(rows)}건 완료 · {raw["output_throughput"]:.2f} 출력 토큰/초 · p95 TTFT {raw["p95_ttft_ms"]:.1f} ms · p95 ITL {raw["p95_itl_ms"]:.1f} ms')
    print("서빙 분석 → 측정 파일 열기:", dest/"runs.csv")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--ollama-model", default="gemma4:e4b", help="이미 저장된 Ollama 모델 태그")
    src.add_argument("--gguf", type=Path, help="로컬 GGUF 텍스트 모델")
    parser.add_argument("--server-bin", help="llama-server 실행 파일")
    parser.add_argument("--out", type=Path, default=ROOT/"results")
    parser.add_argument("--requests", type=int, default=5, help="워밍업 이후 측정할 순차 요청 수")
    try:
        run(parser.parse_args())
    except (OSError, ValueError, KeyError, RuntimeError, http.client.HTTPException, subprocess.SubprocessError) as exc:
        sys.exit(str(exc))
