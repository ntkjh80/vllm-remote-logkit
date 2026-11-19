"""분석 PC에서 kubectl을 이용해 Pod 한 개의 엔진 지표와 로그를 수집한다."""
import http.client
import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from analyze_logs import summarize
from worker import OOM, Sampling, log_events, save, utc


def add_parser(sub):
    p = sub.add_parser("kube", help="kubectl로 운영 중인 Pod 관측")
    p.add_argument("-n", "--namespace", required=True, help="Pod 네임스페이스")
    p.add_argument("--pod", required=True, help="관측할 Pod 이름 한 개")
    p.add_argument("-c", "--container", help="로그를 읽을 컨테이너. 1개면 자동 선택")
    p.add_argument("--context", help="kubeconfig context. 생략하면 현재 context")
    p.add_argument("--port", type=int, default=8000, help="Pod의 vLLM HTTP 포트 (기본 8000)")
    p.add_argument("--seconds", type=int, default=30, help="관측 시간 (기본 30초)")
    p.add_argument("--interval", type=int, default=2, help="메트릭 수집 간격 (기본 2초)")
    p.add_argument("--logs-only", action="store_true", help="포트 포워딩 없이 로그만 수집")
    p.add_argument("--out", type=Path, default=Path("results"), help="결과 폴더 (기본 results)")


def kubectl(args):
    try:
        result = subprocess.run(["kubectl", *args], capture_output=True, timeout=30)
    except FileNotFoundError:
        raise RuntimeError("분석 PC에 kubectl을 설치하고 터미널의 PATH를 확인하세요.") from None
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip() or "kubectl 명령 실패")
    return result.stdout


def identity(pod, container):
    statuses = pod["status"].get("containerStatuses", [])
    status = next((s for s in statuses if s["name"] == container), None)
    if not status or not status.get("containerID"):
        raise ValueError("컨테이너 실행 ID를 확인할 수 없습니다. kubectl get pod로 상태를 확인하세요.")
    return {"pod_uid": pod["metadata"]["uid"], "node": pod["spec"].get("nodeName"),
            "container_id": status["containerID"], "image": status.get("image"),
            "image_id": status.get("imageID"), "restart_count": status["restartCount"]}


def choose_container(pod, name):
    names = [c["name"] for c in pod["spec"]["containers"]]
    if name in names:
        return name
    if name is None and len(names) == 1:
        return names[0]
    raise ValueError("로그를 읽을 컨테이너를 -c 로 지정하세요. 선택 가능: " + ", ".join(names))


@contextmanager
def forward(args, pod, port):
    proc = subprocess.Popen(
        ["kubectl", *args, "port-forward", "--address", "127.0.0.1", "pod/" + pod, ":" + str(port)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env={**os.environ, "LC_ALL": "C"})
    # 연결 안내를 받은 뒤에도 계속 읽어 kubectl의 출력 버퍼가 차지 않게 한다.
    events = queue.Queue()

    def drain():
        last = ""
        for line in proc.stdout:
            match = re.match(r"Forwarding from 127\.0\.0\.1:(\d+) ->", line)
            if match:
                events.put((int(match[1]), ""))
            last = line.strip()
        events.put((None, last or "kubectl port-forward 종료"))

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        try:
            local_port, error = events.get(timeout=20)
        except queue.Empty:
            raise RuntimeError("port-forward 연결 대기 20초 초과") from None
        if error:
            raise RuntimeError(error)
        yield "http://127.0.0.1:" + str(local_port)
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        reader.join()
        proc.stdout.close()


def local_metrics(url):
    # 로컬 포워딩에만 GET을 전송한다. PC의 프록시나 API 키는 사용하지 않는다.
    conn = http.client.HTTPConnection("127.0.0.1", urlsplit(url).port, timeout=3)
    try:
        conn.request("GET", "/metrics")
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError(f"/metrics HTTP {response.status}")
        return response.read().decode("utf-8", "replace")
    except http.client.HTTPException as exc:
        raise ValueError(str(exc)) from exc
    finally:
        conn.close()


def collect_logs(args, pod, container, start, dest):
    # 내 PC에만 저장한다. 요약 JSON에는 원문 대신 일치한 줄 수를 넣는다.
    raw = kubectl([*args, "logs", pod, "-c", container, "--timestamps=true",
                   "--since-time=" + start, "--tail=2000", "--limit-bytes=2097152"])
    path = dest / "pod.log"
    path.write_bytes(raw)
    events = dest / "events.log"
    log_events(path, events)
    lines = events.read_text(encoding="utf-8").splitlines()
    return {"lines": len(raw.splitlines()), "events": len(lines),
            "oom_lines": sum(bool(OOM.search(line)) for line in lines)}


def run(a):
    if min(a.seconds, a.interval) < 1 or not 1 <= a.port <= 65535:
        raise ValueError("시간과 수집 간격은 1초 이상, 포트는 1~65535로 입력하세요.")
    for name in (a.pod, a.namespace, a.container):
        if name is not None and not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", name):
            raise ValueError("kubectl get pod에 표시되는 네임스페이스·Pod·컨테이너 이름을 입력하세요.")
    context = a.context or kubectl(["config", "current-context"]).decode().strip()
    if not context:
        raise ValueError("현재 Kubernetes context가 비어 있습니다. --context로 지정하세요.")
    args = ["--context", context, "--namespace", a.namespace]
    get_pod = [*args, "get", "pod", a.pod, "-o", "json"]
    pod = json.loads(kubectl(get_pod))
    container = choose_container(pod, a.container)
    before = identity(pod, container)
    dest = a.out / ("kube-" + uuid.uuid4().hex[:12])
    dest.mkdir(parents=True)
    target = dict(context=context, namespace=a.namespace, pod=a.pod, container=container, port=a.port)
    warnings = []
    start = utc()
    metrics = logs = after = None
    errors = (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError)
    print(f"{context} / {a.namespace} / {a.pod} ({container}) · {a.seconds}초 관측", flush=True)
    try:
        if a.logs_only:
            time.sleep(a.seconds)
        else:
            try:
                with forward(args, a.pod, a.port) as url:
                    start = utc()
                    with Sampling(url, dest, a.interval, fetch=local_metrics) as samples:
                        time.sleep(a.seconds)
                    metrics = summarize(dest / "metrics.jsonl.gz")
                    if samples.failed:
                        warnings.append("메트릭 수집 오류: " + str(samples.last_error))
                    if not metrics["series"]:
                        warnings.append("요약할 엔진 지표가 없습니다. Pod 포트와 /metrics 응답을 확인하세요.")
            except errors as exc:
                warnings.append("포트 포워딩 또는 메트릭 수집 실패: " + str(exc))
                if not (dest / "metrics.jsonl.gz").exists():
                    start = utc()
                    time.sleep(a.seconds)  # 포트 연결 실패 시에도 같은 길이의 로그 구간을 확보
        try:
            logs = collect_logs(args, a.pod, container, start, dest)
        except errors as exc:
            warnings.append("컨테이너 로그 수집 실패: " + str(exc))
        try:
            after = identity(json.loads(kubectl(get_pod)), container)
            if before != after:
                warnings.append("관측 중 Pod 또는 컨테이너가 바뀌었습니다. 새로 측정하세요.")
        except errors as exc:
            warnings.append("종료 시 Pod 식별 확인 실패: " + str(exc))
    except KeyboardInterrupt:
        warnings.append("사용자가 관측을 중단했습니다.")
    verified = after == before
    usable = verified and (logs is not None or bool(metrics and metrics["series"]))
    status = ("partial" if warnings else "ok") if usable else "invalid"
    report = {
        "kind": "vllm-kubectl-observation/v1", "data_kind": "measured", "status": status, "target": target,
        "start_utc": start, "end_utc": utc(), "logs_only": a.logs_only,
        "seconds": a.seconds, "interval_s": a.interval, "identity_verified": verified,
        "identity_before": before, "identity_after": after,
        "metrics": metrics if verified else None, "logs": logs if verified else None, "warnings": warnings,
    }
    path = dest / "kubectl-summary.json"
    save(path, report)
    for warning in warnings:
        print(warning)
    print(f"HTML → 원격 수집 → Pod 관측 파일 열기\n{path}")
    return 0 if status == "ok" else 1
