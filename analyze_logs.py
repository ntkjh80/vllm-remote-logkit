#!/usr/bin/env python3
"""Prometheus 시계열 요약 및 커널 측정값의 HTML 단위 변환."""
import argparse
import csv
import gzip
import json
import math
import re
from pathlib import Path
from worker import save

LINE=re.compile(r'^(vllm:\w+)(\{(?:[^"\\}]|\\.|"(?:\\.|[^"\\])*")*\})?\s+(\S+)')
GAUGE={"num_requests_waiting","num_requests_running","kv_cache_usage_perc","gpu_cache_usage_perc"}
COUNT={"num_preemptions","prompt_tokens","generation_tokens","prefix_cache_hits","prefix_cache_queries"}


def summarize(src):
    result={}
    ok=bad=0
    with gzip.open(src,"rt",encoding="utf8") as f:
        for line in f:
            rec=json.loads(line)
            if "error" in rec:
                bad+=1
                continue
            ok+=1
            for s in rec["samples"]:
                m=LINE.match(s)
                if not m:
                    continue
                name=m[1].split(":",1)[1]
                counter=name.removesuffix("_total") in COUNT
                if name not in GAUGE and not counter:
                    continue
                v=float(m[3])
                if not math.isfinite(v):
                    continue
                key=m[1]+(m[2] or "")
                if key not in result:
                    result[key]=dict(first=v,last=v,min=v,max=v,samples=1,first_utc=rec["utc"],last_utc=rec["utc"],
                                     observed_delta=0 if counter else None,resets=0)
                    continue
                r=result[key]
                if counter:
                    r["resets"]+=int(v<r["last"])
                    r["observed_delta"]+=v if v<r["last"] else v-r["last"]
                r.update(last=v,min=min(r["min"],v),max=max(r["max"],v),last_utc=rec["utc"],samples=r["samples"]+1)
    return dict(ok=ok,failed=bad,series=result)


def roof(a):
    values=[]
    with a.source.open(encoding="utf-8-sig",newline="") as f:
        for i,r in enumerate(csv.DictReader(f),2):
            try:
                flop,traffic,ms=[float(r[k].replace(",","").strip()) for k in (a.flops,a.bytes,a.time)]
                ms*=dict(ns=1e-6,us=.001,ms=1,s=1000)[a.time_unit]
                if not all(math.isfinite(x) and x>0 for x in (flop,traffic,ms,a.peak,a.bandwidth)):
                    raise ValueError("양수 필요")
                intensity=flop/traffic
                actual=flop/1e9/ms
                ceiling=min(a.peak,a.bandwidth*intensity)
                values.append(dict(run_id=a.run_id,operation=r[a.name],phase=a.phase,scope=a.scope,flop_source=a.flop_source,
                                   peak_tflops=a.peak,hbm_tb_s=a.bandwidth,ops_gflop=flop/1e9,hbm_gb=traffic/1e9,
                                   time_ms=ms,flop_per_byte=intensity,actual_tflops=actual,roof_tflops=ceiling,
                                   percent_of_roof=100*actual/ceiling,check="check_scope_units" if actual>ceiling else "ok"))
            except (KeyError,ValueError,TypeError,AttributeError) as e:
                raise ValueError(f"{i}행의 열이나 단위를 확인하세요: {e}") from e
    if not values:
        raise ValueError("측정 행 없음")
    return values


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest="action",required=True)
    q=sub.add_parser("metrics")
    q.add_argument("source",type=Path)
    q=sub.add_parser("roof")
    q.add_argument("source",type=Path)
    for k in ("name","flops","bytes","time","phase","scope","flop-source","run-id"):
        q.add_argument("--"+k,required=True)
    q.add_argument("--time-unit",choices=["s","ms","us","ns"],required=True)
    q.add_argument("--peak",type=float,required=True)
    q.add_argument("--bandwidth",type=float,required=True)
    p.add_argument("--out",type=Path,required=True,help="하위 명령 앞에 지정")
    a=p.parse_args()
    if a.out.resolve()==a.source.resolve():
        p.error("원본과 다른 출력 경로를 지정하세요")
    a.out.parent.mkdir(parents=True,exist_ok=True)
    if a.action=="metrics":
        save(a.out,summarize(a.source))
    else:
        rows=roof(a)
        with a.out.open("w",encoding="utf-8-sig",newline="") as f:
            w=csv.DictWriter(f,fieldnames=rows[0])
            w.writeheader()
            w.writerows(rows)
