#!/usr/bin/env python3
"""원격 Linux VM/Pod에서 실행한다. Python 3.9+ 표준 라이브러리."""
import csv
import gzip
import json
import math
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from importlib import metadata
from pathlib import Path

FIELDS = ("run_id scenario model dtype environment input_tokens output_tokens concurrency "
          "max_num_seqs max_num_batched_tokens max_model_len status quality p95_ttft_ms "
          "p95_itl_ms output_throughput request_throughput data_kind").split()
KNOBS = FIELDS[8:11]
METRICS = FIELDS[13:17]
FLAGS = ["--" + k.replace("_", "-") for k in KNOBS]
ENV = "CUDA_VISIBLE_DEVICES NVIDIA_VISIBLE_DEVICES PJRT_DEVICE TPU_ACCELERATOR_TYPE TPU_WORKER_ID VLLM_USE_V1 VLLM_TARGET_DEVICE VLLM_PLUGINS VLLM_LOGGING_LEVEL HF_HUB_OFFLINE".split()
OOM = re.compile(r"out of memory|OutOfMemoryError|RESOURCE_EXHAUSTED.*(?:memory|alloc)", re.I)
EVENT = re.compile(r"max_num_seqs|max_num_batched_tokens|max_model_len|non-default args|Initializing a .*engine|KV cache|compil(?:ing|ation)|loading .*weights|out of memory|OutOfMemoryError|RESOURCE_EXHAUSTED",re.I)


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def save(p, obj):
    p = Path(p)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False),encoding="utf8")
    os.replace(tmp,p)


def read(p):
    return json.loads(Path(p).read_text(encoding="utf-8-sig"))


def digest(obj):
    return sha256(json.dumps(obj,sort_keys=True).encode()).hexdigest()[:16]


def get(url, timeout=3):
    key = os.environ.get("OPENAI_API_KEY")
    req = urllib.request.Request(url,headers={"Authorization":"Bearer "+key} if key else {})
    with urllib.request.urlopen(req,timeout=timeout) as r:
        return r.read().decode("utf8","replace")


def host_info():
    packages={}
    for name in ("vllm","torch","transformers","tokenizers","torch-xla","torch-tpu","libtpu","tpu-inference","vllm-torchtpu","jax","jaxlib"):
        try:
            packages[name]=metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    facts={"host":platform.node(),"os":platform.platform(),"python":sys.executable,"packages":packages,
           "env":{k:os.environ[k] for k in ENV if k in os.environ},"utc":utc()}
    for tool,argv in [("nvidia-smi",["--query-gpu=index,name,uuid,driver_version,memory.total","--format=csv"]),("tpu-info",[])]:
        if shutil.which(tool):
            try:
                p=subprocess.run([tool]+argv,capture_output=True,text=True,timeout=8)
                facts[tool]={"returncode":p.returncode,"output":(p.stdout+p.stderr)[:32000]}
            except (OSError,subprocess.TimeoutExpired) as e:
                facts[tool]={"error":str(e)}
    return facts


def log_events(src,dst):
    if not Path(src).is_file():
        return
    with Path(src).open(errors="replace") as f, Path(dst).open("w",encoding="utf8") as out:
        for n,line in enumerate(f,1):
            if EVENT.search(line):
                out.write(f"{n}\t{line}")


class Sampling:
    """엔진 라벨을 유지한다. 누락된 /metrics 값은 결측으로 남긴다."""
    def __init__(self,url,dest,interval,fetch=None):
        self.url,self.dest,self.interval=url,dest,interval
        self.fetch=fetch or get
        self.last_error=None
        self.stop=threading.Event()
        self.ok=self.failed=0

    def once(self):
        row={"utc":utc()}
        try:
            lines=[s for s in self.fetch(self.url+"/metrics").splitlines() if s.startswith("vllm:")]
            if not lines:
                raise ValueError("vllm metric 없음")
            row["samples"]=lines
            self.ok+=1
        except (OSError,ValueError) as e:
            self.last_error=row["error"]=str(e)
            self.failed+=1
        self.file.write(json.dumps(row,ensure_ascii=False)+"\n")

    def loop(self):
        while not self.stop.wait(self.interval):
            self.once()

    def __enter__(self):
        self.file=gzip.open(self.dest/"metrics.jsonl.gz","wt",encoding="utf8")
        self.once()
        self.thread=threading.Thread(target=self.loop,daemon=True)
        self.thread.start()
        return self

    def __exit__(self,*exc):
        self.stop.set()
        self.thread.join()
        try:
            self.once()
        finally:
            self.file.close()
        save(self.dest/"scrapes.json",{"ok":self.ok,"failed":self.failed,"interval_s":self.interval})


@contextmanager
def child(argv,log):
    # 자신이 시작한 프로세스 그룹만 종료한다. attach 경로에는 서버 프로세스가 없다.
    with Path(log).open("wb") as f:
        proc=subprocess.Popen(argv,stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
        try:
            yield proc
        finally:
            proc.poll()
            try:
                os.killpg(proc.pid,signal.SIGTERM)
                end=time.monotonic()+6
                while time.monotonic()<end:
                    proc.poll()
                    os.killpg(proc.pid,0)
                    time.sleep(.15)
                os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()


def lookup(url,alias,context_len,proc=None,timeout=900):
    end=time.monotonic()+timeout
    while True:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError("서버 기동 실패, server.log 확인")
        try:
            models=json.loads(get(url+"/v1/models"))["data"]
            entry=next((m for m in models if m.get("id")==alias),None)
            if entry is None:
                raise ValueError("served_name 불일치: "+str([m.get("id") for m in models]))
            if entry.get("max_model_len") is not None and int(entry["max_model_len"])!=context_len:
                raise ValueError("서버의 max_model_len과 설정 파일 불일치")
            return models
        except OSError:
            if proc is None or time.monotonic()>=end:
                raise
            time.sleep(1)


def vllm_bin(t):
    f=t.get("vllm_bin") or shutil.which("vllm") or str(Path(sys.executable).with_name("vllm"))
    if not os.access(f,os.X_OK):
        raise ValueError("원격 환경에서 vllm 실행 파일을 찾을 수 없습니다. vllm_bin을 지정하세요.")
    return f


@contextmanager
def serving(action,t,cfg,dest):
    url=t["url"].rstrip("/")
    if action=="collect":
        save(dest/"ready.json",{"utc":utc(),"models":lookup(url,t["served_name"],cfg["max_model_len"]),
                               "settings_source":"operator-declared"})
        yield url,None
        return
    from urllib.parse import urlsplit
    parsed=urlsplit(url)
    if parsed.scheme!="http" or parsed.hostname!="127.0.0.1" or parsed.path not in ("","/"):
        raise ValueError("launch URL은 http://127.0.0.1:PORT 형식입니다")
    port=parsed.port or 80
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",port))
    cmd=[vllm_bin(t),"serve",t["model_path"],"--served-model-name",t["served_name"],"--tokenizer",t["tokenizer_path"],
         "--host","127.0.0.1","--port",str(port),"--dtype",t["dtype"],"--tensor-parallel-size",str(t["tp"]),
         "--data-parallel-size",str(t["dp"])]
    for k,flag in zip(KNOBS,FLAGS):
        cmd.extend([flag,str(cfg[k])])
    if t["quantization"] not in ("none","model_config","auto"):
        cmd.extend(["--quantization",t["quantization"]])
    cmd+=t.get("server_args",[])
    started=time.monotonic()
    save(dest/"launch.json",{"utc":utc(),"argv":cmd})
    try:
        with child(cmd,dest/"server.log") as p:
            models=lookup(url,t["served_name"],cfg["max_model_len"],p,t.get("startup_timeout",1800))
            save(dest/"ready.json",{"utc":utc(),"models":models,"launch_to_ready_s":time.monotonic()-started,
                                    "settings_source":"launcher"})
            yield url,p
    finally:
        log_events(dest/"server.log",dest/"server-events.log")


def cmd_bench(t,s,c,url,dest,stem,count):
    # API에는 served_name을, 클라이언트에는 장비 쪽에 있는 고정 토크나이저 경로를 사용한다.
    return [vllm_bin(t),"bench","serve","--backend","openai","--endpoint","/v1/completions","--base-url",url,
            "--model",t.get("model_path") or t["served_name"],"--served-model-name",t["served_name"],
            "--tokenizer",t["tokenizer_path"],"--dataset-name","random",
            "--random-input-len",str(s["input_tokens"]),"--random-output-len",str(s["output_tokens"]),
            "--random-range-ratio","0","--ignore-eos","--seed",str(s["seed"]),"--request-rate","inf",
            "--num-prompts",str(count),"--max-concurrency",str(c),"--percentile-metrics","ttft,itl",
            "--metric-percentiles","95,99","--save-result","--result-dir",str(dest.resolve()),"--result-filename",stem+".json"]


def base_row(meta):
    row={k:meta[k] for k in FIELDS[:11]}
    for k in FIELDS[:5]:
        if not isinstance(row[k],str) or not row[k].strip():
            raise ValueError("식별값 누락: "+k)
    for k in FIELDS[5:11]:
        if type(row[k]) is not int or row[k]<1:
            raise ValueError("양의 정수 필요: "+k)
    row.update(status="ok",quality=meta.get("quality","unknown"),data_kind=meta.get("data_kind","measured"))
    if row["quality"] not in ("pass","fail","unknown") or row["data_kind"] not in ("demo","measured"):
        raise ValueError("quality/data_kind 확인")
    return row


def normalize(meta,raw):
    row=base_row(meta)
    if raw.get("max_concurrency") is not None and int(raw["max_concurrency"])!=row["concurrency"]:
        raise ValueError("원본과 concurrency 불일치")
    count=raw.get("completed")
    if type(count) is not int or count<=0 or raw.get("failed",0)!=0:
        raise ValueError("실패 또는 미완료 요청 있음")
    expected=meta.get("num_prompts",raw.get("num_prompts"))
    if expected is None and raw.get("failed") is None:
        raise ValueError("실패/계획 요청 수 미상")
    if expected is not None and (int(expected)!=count or int(raw.get("num_prompts",expected))!=count):
        raise ValueError("완료 요청 수 불일치")
    for k in METRICS:
        v=raw.get(k,raw.get(k.replace("p95_","p95.0_")))
        if k=="request_throughput" and v is None:
            row[k]=None
            continue
        if isinstance(v,bool) or v is None or not math.isfinite(float(v)) or float(v)<=0:
            raise ValueError("유효한 실측값 필요: "+k)
        row[k]=float(v)
    return row


def phase(t,s,c,url,d,stem,count):
    argv=cmd_bench(t,s,c,url,d,stem,count)
    save(d/(stem+"-command.json"),{"utc":utc(),"argv":argv})
    with child(argv,d/(stem+".log")) as p:
        if p.wait(timeout=t.get("bench_timeout",1800)):
            raise RuntimeError(stem+" 실행 실패")
    return read(d/(stem+".json"))


def run_once(t,cfg,s,c,rep,dest,url,facts):
    rid=uuid.uuid4().hex
    d=dest/rid
    d.mkdir()
    workload={k:s[k] for k in ("seed","input_tokens","output_tokens","warmup_prompts","num_prompts")}
    workload.update(tokenizer=t["tokenizer_revision"],workload="random-fixed-ignore-eos-rate-inf")
    fixed={k:t[k] for k in ("hardware","devices","tp","dp","server_build","backend","quantization","fixed_options")}
    fixed.update(extra=t.get("server_args",[]),env=facts["env"],versions=facts["packages"],image=t.get("observed_image_id"))
    label=f"{t['hardware']}×{t['devices']} {t['backend']} TP{t['tp']}/DP{t['dp']} {t['server_build']} #{digest(fixed)}"
    meta=dict(run_id=rid,scenario=s["id"]+" #"+digest(workload),model=t["model_id"]+"@"+t["revision"],
              dtype=t["dtype"],environment=label,input_tokens=s["input_tokens"],output_tokens=s["output_tokens"],
              concurrency=c,**{k:cfg[k] for k in KNOBS},num_prompts=s["num_prompts"],quality="unknown",
              data_kind="measured",begin_utc=utc(),repeat=rep)
    save(d/"run.json",meta)
    stage="warmup"
    try:
        if s["warmup_prompts"]:
            warm=phase(t,s,c,url,d,"warmup",s["warmup_prompts"])
            normalize(dict(meta,num_prompts=s["warmup_prompts"]),warm)
        stage="bench"
        with Sampling(url,d,t.get("interval",2)):
            meta["bench_begin_utc"]=utc()
            raw=phase(t,s,c,url,d,"bench",s["num_prompts"])
            meta["bench_end_utc"]=utc()
        row=normalize(meta,raw)
    except (ValueError,TypeError,KeyError,OSError,RuntimeError,subprocess.TimeoutExpired) as e:
        row={k:meta[k] for k in FIELDS if k in meta}
        log=d/(stage+".log")
        with log.open(errors="replace") if log.exists() else open(os.devnull) as f:
            oom=any(OOM.search(line) for line in f)
        row.update(status="oom" if oom else "error",**dict.fromkeys(METRICS))
        save(d/"error.json",{"stage":stage,"error":str(e)})
    meta["end_utc"]=utc()
    save(d/"run.json",meta)
    save(d/"html.json",row)
    return row


def process_info(pid):
    """서버 PID의 허용된 옵션과 환경만 수집한다. 다른 CLI 옵션·인증값은 저장하지 않는다."""
    if type(pid) is not int or pid<=0:
        raise ValueError("server_pid는 원격 컨테이너/VM 안의 양수 PID입니다")
    proc=Path("/proc")/str(pid)
    argv=proc.joinpath("cmdline").read_bytes().decode(errors="replace").split("\0")
    allow=set(FLAGS+["--tensor-parallel-size","--data-parallel-size","--dtype","--kv-cache-dtype",
                     "--kv-cache-memory-bytes","--gpu-memory-utilization","--quantization","--revision","--tokenizer-revision"])
    options={}
    for i,tok in enumerate(argv):
        k,sep,v=tok.partition("=")
        if k in allow:
            options[k]=v if sep else (argv[i+1] if i+1<len(argv) else None)
    obj={"pid":pid,"explicit_options":options}
    try:
        entries=proc.joinpath("environ").read_bytes().decode(errors="replace").split("\0")
        obj["env"]={p.partition("=")[0]:p.partition("=")[2] for p in entries if p.partition("=")[0] in ENV}
    except OSError as e:
        obj["environment_error"]=str(e)
    return obj


def service_logs(t,out):
    if t.get("server_log"):
        src=Path(t["server_log"])
        if src.is_file():
            log_events(src,out/"existing-server-events.log")
        else:
            save(out/"server-log-error.json",{"error":"원격 로그 경로 없음","path":str(src)})
    if t.get("journal_unit"):
        try:
            with (out/"journal.log").open("wb") as f:
                rc=subprocess.run(["journalctl","--no-pager","-n","20000","-o","short-iso","-u",t["journal_unit"]],stdout=f,stderr=subprocess.STDOUT,timeout=30).returncode
            save(out/"journal-status.json",{"returncode":rc})
            log_events(out/"journal.log",out/"journal-events.log")
        except (OSError,subprocess.TimeoutExpired) as e:
            save(out/"journal-status.json",{"error":str(e)})

def work(job,out):
    t=job["target"]
    out.mkdir(parents=True,exist_ok=True)
    facts=host_info()
    save(out/"host.json",facts)
    save(out/"manifest.json",job)
    if t.get("server_pid") and job["action"]!="launch":
        procinfo=process_info(t["server_pid"])
        save(out/"service-process.json",procinfo)
        for cfg in job.get("settings",[]):
            for k,flag in zip(KNOBS,FLAGS):
                explicit=procinfo["explicit_options"].get(flag)
                if explicit and explicit.isdigit() and int(explicit)!=cfg[k]:
                    raise ValueError(f"실제 PID의 {flag}={explicit}, 선언값={cfg[k]}")
    if job["action"]=="observe":
        with Sampling(t["url"].rstrip("/"),out,t.get("interval",2)) as sampler:
            time.sleep(job["seconds"])
        service_logs(t,out)
        return int(sampler.ok==0)
    errors=0
    for cfg in job["settings"]:
        d=out/cfg["id"]
        d.mkdir()
        try:
            with serving(job["action"],t,cfg,d) as (url,proc):
                for s in job["scenarios"]:
                    for c in s["concurrency"]:
                        for rep in range(1,s["repeats"]+1):
                            if proc and proc.poll() is not None:
                                raise RuntimeError("서버 종료. 남은 부하는 미실행")
                            row=run_once(t,cfg,s,c,rep,d,url,facts)
                            errors+=row["status"]!="ok"
        except (ValueError,OSError,RuntimeError,KeyError) as e:
            errors+=1
            save(d/"setting-error.json",{"utc":utc(),"error":str(e)})
    service_logs(t,out)
    return int(errors>0)


if __name__=="__main__":
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(143))
    dest=Path(sys.argv[2])
    rc=1
    try:
        rc=work(read(sys.argv[1]),dest)
    finally:
        save(dest.parent/"done.json",{"utc":utc(),"returncode":rc})
    sys.exit(rc)
