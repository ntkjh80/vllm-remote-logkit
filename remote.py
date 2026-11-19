#!/usr/bin/env python3
"""분석 PC에서 실행하는 원격 수집기. 모델이나 vLLM 설치가 필요하지 않다."""
import argparse
import csv
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import kube_observe
from worker import FIELDS, KNOBS, FLAGS, base_row, log_events, normalize, read, save, utc

ROOT=Path(__file__).resolve().parent
ID=re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")
# 고유 작업 공간에서만 파일을 작성한다. 내려받은 뒤에도 재수거가 가능하다.
START="""import json,os,sys,tempfile,subprocess
from pathlib import Path
p=json.load(sys.stdin)
d=Path(tempfile.mkdtemp(prefix='vllm-logkit-',dir=p.get('tmp') or None))
(d/'worker.py').write_text(p['worker'],encoding='utf8')
(d/'job.json').write_text(json.dumps(p['job']),encoding='utf8')
with (d/'worker.log').open('wb') as f:
    child=subprocess.Popen([sys.executable,str(d/'worker.py'),str(d/'job.json'),str(d/'results')],stdout=f,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,start_new_session=True)
print(json.dumps({'path':str(d),'pid':child.pid}))
"""
POLL="""import json,os,sys
from pathlib import Path
d=Path(sys.argv[1]); p=d/'done.json'
if p.exists(): print(p.read_text())
else:
    try: os.kill(int(sys.argv[2]),0); print('{"status":"running"}')
    except ProcessLookupError: print('{"status":"stopped_without_marker"}')
"""
PACK="""import sys,zipfile
from pathlib import Path
d=Path(sys.argv[1])
with zipfile.ZipFile(sys.stdout.buffer,'w',compression=zipfile.ZIP_DEFLATED,allowZip64=True) as z:
    for p in sorted(d.rglob('*')):
        if p.is_file() and not p.is_symlink() and p.name!='worker.py' and not p.name.endswith('.tmp'):
            z.write(p,p.relative_to(d).as_posix())
"""


def kub(c):
    return ["kubectl","--context",c["context"],"--namespace",c["namespace"]]


def remote(c,argv):
    if c["type"]=="k8s":
        return kub(c)+["exec","-i",c["pod"],"-c",c["container"],"--"]+argv
    host=c["host"]
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]*",host):
        raise ValueError("SSH host에는 SSH 설정 별칭 또는 user@hostname을 입력하세요")
    return ["ssh","-T","-o","BatchMode=yes","-o","ConnectTimeout=15",host,shlex.join(argv)]


def pod_meta(c):
    raw=subprocess.check_output(kub(c)+["get","pod",c["pod"],"-o","json"])
    p=json.loads(raw)
    return kube_observe.identity(p,c["container"])


def index(items):
    out={}
    for v in items:
        k=v["id"]
        if not ID.fullmatch(k) or k in out:
            raise ValueError("목록의 ID가 중복되거나 형식이 잘못됐습니다")
        out[k]=v
    if not out:
        raise ValueError("목록이 비어 있습니다")
    return out


def integer(v,lo=1):
    if type(v) is not int or v<lo:
        raise ValueError(f"{lo} 이상의 정수가 필요합니다: {v!r}")


def make_jobs(args):
    conf=read(args.config)
    targets=index(conf["targets"])
    scenarios=index(conf.get("scenarios",[])) if args.action!="observe" else {}
    for name in args.target or targets:
        t=targets[name]
        c=t["connection"]
        if c["type"]=="k8s":
            for field in ("context","namespace","pod","container"):
                if not c.get(field) or c[field].startswith("-"):
                    raise ValueError("Kubernetes 연결 필드 확인: "+field)
        elif c["type"]!="ssh":
            raise ValueError("connection.type: ssh 또는 k8s")
        u=urlsplit(t["url"])
        if u.scheme not in ("http","https") or not u.hostname or u.username or u.password or u.query or u.fragment or u.path.rstrip("/").endswith("/v1"):
            raise ValueError("url에는 인증 정보 없이 vLLM의 기본 HTTP 주소를 입력하세요")
        if c["type"]=="k8s" and u.hostname not in ("127.0.0.1","localhost","::1"):
            raise ValueError("Pod 안의 루프백 URL로 대상 replica를 고정하세요")
        if t.get("interval",2)<=0:
            raise ValueError("interval > 0")
        if args.action=="observe":
            integer(args.seconds)
            yield {"action":"observe","target":t,"seconds":args.seconds}
            continue
        for field in ("served_name","model_id","revision","dtype","hardware","backend","server_build",
                      "tokenizer_path","tokenizer_revision","fixed_options","quantization"):
            if not isinstance(t.get(field),str) or not t[field].strip():
                raise ValueError(f"{name}: {field} 필요")
        if args.action=="launch" and not t.get("model_path"):
            raise ValueError("launch에는 원격 model_path가 필요합니다")
        for k in ("tp","dp","devices"):
            integer(t[k])
        protected=set(FLAGS+["--model","--tokenizer","--port","--host","--dtype","--served-model-name",
                             "--tensor-parallel-size","--data-parallel-size","-tp","-dp","--api-key","--config","--quantization","-q"])
        if any(a.split("=")[0] in protected for a in t.get("server_args",[])):
            raise ValueError("server_args의 중복 옵션 또는 인증정보를 제거하세요")
        settings=index(t["settings"])
        if args.action=="collect" and not args.setting and len(settings)>1:
            raise ValueError(f"{name}: 이미 떠 있는 서버의 실제 --setting 하나를 지정하세요")
        selected=[settings[args.setting]] if args.setting else list(settings.values())
        sc=[scenarios[n] for n in args.scenario or scenarios]
        for cfg in selected:
            for k in KNOBS:
                integer(cfg[k])
            for s in sc:
                for k in ("input_tokens","num_prompts","repeats"):
                    integer(s[k])
                integer(s["output_tokens"],2)
                integer(s["warmup_prompts"],0)
                integer(s["seed"],0)
                if not s["concurrency"] or len(set(s["concurrency"]))!=len(s["concurrency"]):
                    raise ValueError("동시 요청 수 목록 확인")
                for n in s["concurrency"]:
                    integer(n)
                    if n>s["num_prompts"]:
                        raise ValueError("concurrency > num_prompts")
                if s["input_tokens"]+s["output_tokens"]>cfg["max_model_len"]:
                    raise ValueError("입력+출력 길이가 문맥 상한을 초과합니다")
        yield {"action":args.action,"target":t,"settings":selected,"scenarios":sc}


def retrieve(receipt,partial=False):
    r=read(receipt)
    c=r["connection"]
    d=receipt.parent
    identity=pod_meta(c) if c["type"]=="k8s" else None
    if identity!=r.get("pod_identity"):
        raise ValueError("Pod UID 또는 컨테이너가 바뀌었습니다. 다른 실행의 결과를 결합할 수 없습니다.")
    interp=r["python"]
    status=json.loads(subprocess.check_output(remote(c,[interp,"-c",POLL,r["path"],str(r["pid"])])))
    if "returncode" not in status and not partial:
        if status.get("status")=="running":
            return None
        raise RuntimeError("종료 마커 없이 중단됨. fetch --partial 로 진단 자료를 회수하세요")
    name="partial" if "returncode" not in status else "collected"
    # 전송이 끊겨도 기존 수거 결과와 섞이지 않도록 새 디렉터리에 완성한 뒤 공개한다.
    final=d/name
    if not final.exists():
        with tempfile.TemporaryDirectory(dir=d) as tmp:
            tmp=Path(tmp)
            archive=tmp/"received.zip"
            with archive.open("wb") as z:
                subprocess.run(remote(c,[interp,"-c",PACK,r["path"]]),stdout=z,check=True)
            stage=tmp/"files"
            stage.mkdir()
            with zipfile.ZipFile(archive) as z:
                for entry in z.infolist():
                    path=PurePosixPath(entry.filename)
                    if path.is_absolute() or ".." in path.parts or "\\" in entry.filename or ":" in entry.filename:
                        raise ValueError("원격 압축 파일에 허용되지 않는 경로가 있습니다")
                    dest=stage.joinpath(*path.parts)
                    dest.parent.mkdir(parents=True,exist_ok=True)
                    with dest.open("xb") as out,z.open(entry) as src:
                        shutil.copyfileobj(src,out)
            if c["type"]=="k8s":
                with (stage/"pod.log").open("wb") as f:
                    p=subprocess.run(kub(c)+["logs",c["pod"],"-c",c["container"],"--timestamps","--tail=20000"],stdout=f,stderr=subprocess.PIPE)
                save(stage/"pod-log-status.json",{"returncode":p.returncode,"stderr":p.stderr.decode(errors="replace")})
                if p.returncode==0:
                    log_events(stage/"pod.log",stage/"pod-events.log")
                if pod_meta(c)!=identity:
                    raise RuntimeError("수거 도중 Pod/컨테이너 교체 감지")
            stage.rename(final)
    rows=sorted(final.rglob("html.json"))
    if rows and name=="collected":
        merge(rows,d/"runs.csv")
    print(final,flush=True)
    return status.get("returncode",1)


def merge(paths,out):
    rows={}
    for p in paths:
        row=read(p)
        for k in FIELDS:
            if k not in row:
                raise ValueError(f"{p}: {k} 누락")
        key=row["run_id"]
        if key in rows and rows[key]!=row:
            raise ValueError("같은 run_id에 서로 다른 결과: "+key)
        rows[key]=row
    if not rows or len({r["data_kind"] for r in rows.values()})>1:
        raise ValueError("입력 없음 또는 가상/실제 데이터 혼합")
    out.parent.mkdir(parents=True,exist_ok=True)
    tmp=out.with_name(out.name+"."+uuid.uuid4().hex)
    try:
        with tmp.open("w",encoding="utf-8-sig",newline="") as f:
            writer=csv.DictWriter(f,fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows.values())
        os.replace(tmp,out)
    finally:
        tmp.unlink(missing_ok=True)


def start(job,out):
    t=job["target"]
    c=t["connection"]
    d=out/(t["id"]+"-"+uuid.uuid4().hex[:12])
    d.mkdir(parents=True)
    ident=pod_meta(c) if c["type"]=="k8s" else None
    if ident:
        job=dict(job,target=dict(t,observed_image_id=ident.get("image_id")))
    interp=c.get("python","python3")
    body={"worker":(ROOT/"worker.py").read_text(encoding="utf8"),"job":job,"tmp":c.get("tmp")}
    data=subprocess.check_output(remote(c,[interp,"-c",START]),input=json.dumps(body).encode())
    r=json.loads(data)
    receipt=d/"receipt.json"
    save(receipt,dict(r,connection=c,python=interp,pod_identity=ident,utc=utc()))
    print("수거 재개 파일:",receipt,"원격 작업 위치:",r["path"],flush=True)
    while True:
        code=retrieve(receipt)
        if code is not None:
            return code
        time.sleep(4)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest="action",required=True)
    kube_observe.add_parser(sub)
    for cmd in ("observe","collect","launch"):
        q=sub.add_parser(cmd)
        q.add_argument("--config",default="configs/targets.json")
        q.add_argument("--target",action="append")
        q.add_argument("--setting")
        q.add_argument("--scenario",action="append")
        q.add_argument("--seconds",type=int,default=60)
        q.add_argument("--plan",action="store_true",help="원격 연결 없이 실행 범위 확인")
        q.add_argument("--out",type=Path,default=Path("results"))
    q=sub.add_parser("fetch")
    q.add_argument("receipt",type=Path)
    q.add_argument("--partial",action="store_true")
    q=sub.add_parser("merge")
    q.add_argument("roots",nargs="+",type=Path)
    q.add_argument("--out",type=Path,required=True)
    q=sub.add_parser("extract")
    q.add_argument("--raw",required=True,type=Path)
    q.add_argument("--meta",required=True,type=Path)
    q.add_argument("--out",required=True,type=Path)
    a=p.parse_args()
    if a.action=="kube":
        return kube_observe.run(a)
    if a.action=="extract":
        raw,meta=read(a.raw),read(a.meta)
        row=base_row(meta)
        try:
            row=normalize(meta,raw)
        except (ValueError,TypeError) as e:
            row.update(status="error",**dict.fromkeys(FIELDS[13:17]))
            print("제외:",e,file=sys.stderr)
        a.out.parent.mkdir(parents=True,exist_ok=True)
        save(a.out,row)
        return int(row["status"]!="ok")
    if a.action=="fetch":
        rc=retrieve(a.receipt,a.partial)
        if rc is None:
            print("아직 실행 중입니다. 완료 후 같은 명령으로 수거하세요.")
            return 2
        return rc
    if a.action=="merge":
        paths=[p for root in a.roots for p in root.rglob("html.json") if "partial" not in p.relative_to(root).parts and "partial" not in root.parts]
        merge(paths,a.out)
        return 0
    jobs=list(make_jobs(a))
    rc=0
    for job in jobs:
        if a.plan:
            n=sum(len(s["concurrency"])*s["repeats"] for s in job.get("scenarios",[]))*len(job.get("settings",[]))
            print(job["action"],job["target"]["id"],job["target"]["connection"]["type"],n,"회 측정")
        else:
            try:
                rc=max(rc,start(job,a.out))
            except (OSError,KeyError,ValueError,RuntimeError,subprocess.SubprocessError) as e:
                print(job["target"]["id"],str(e),file=sys.stderr)
                rc=1
    return rc


if __name__=="__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("중단했습니다. 원격 작업 상태는 receipt.json으로 확인하세요.")
    except (OSError,ValueError,KeyError,RuntimeError,subprocess.SubprocessError) as e:
        sys.exit(str(e))
