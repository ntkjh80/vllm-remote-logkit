"""실장비 대신 모의 SSH/kubectl, HTTP와 vLLM을 사용한다. 작업/전송 프로세스는 실제로 실행한다."""
import contextlib
import csv
import gzip
import io
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import remote
import worker
from analyze_logs import summarize

TRANSPORT = """#!/usr/bin/env python3
import json,os,shlex,sys
from pathlib import Path
a=sys.argv[1:]
if Path(sys.argv[0]).name=='ssh': cmd=shlex.split(a[-1])
elif 'get' in a:
 print(json.dumps({'metadata':{'uid':os.environ.get('TEST_POD_UID','p1')},'spec':{'nodeName':'node'},'status':{'containerStatuses':[{'name':'vllm','containerID':'cid','restartCount':0,'image':'im','imageID':'sha'}]}}));sys.exit()
elif 'logs' in a: print('startup config max_num_seqs=64');sys.exit()
else: cmd=a[a.index('--')+1:]
os.execvp(cmd[0],cmd)
"""
NETWORK = """import io,json,os,urllib.request
from pathlib import Path
def fake(req,timeout=None):
 url=req.full_url
 if url.endswith('/v1/models'):
  if os.environ.get('TEST_LAUNCH') and not Path(os.environ['TEST_READY']).exists(): raise OSError('starting')
  return io.BytesIO(json.dumps({'data':[{'id':'test','max_model_len':8192}]}).encode())
 if url.endswith('/metrics'):
  return io.BytesIO(b'vllm:num_requests_running{engine="0"} 3\\nvllm:kv_cache_usage_perc{engine="0"} 0.25\\n')
 raise ValueError(url)
urllib.request.urlopen=fake
"""
CLI = """#!/usr/bin/env python3
import json,os,sys,time
from pathlib import Path
a=sys.argv[1:]
def arg(x):return a[a.index(x)+1]
if a[0]=='serve':
 Path(os.environ['TEST_READY']).write_text(str(os.getpid()))
 print('GPU KV cache size: 1024 tokens',flush=True)
 time.sleep(90)
elif a[:2]==['bench','serve']:
 n=int(arg('--num-prompts')); raw={'num_prompts':n,'completed':n,'failed':0,'max_concurrency':int(arg('--max-concurrency')),'p95_ttft_ms':120,'p95_itl_ms':25,'output_throughput':640,'request_throughput':5}
 if os.environ.get('TEST_FAIL') and arg('--result-filename')=='bench.json':raw['failed']=1;raw['completed']-=1
 (Path(arg('--result-dir'))/arg('--result-filename')).write_text(json.dumps(raw))
else:sys.exit(2)
"""


class RemoteTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp=Path(tmp.name)
        for name,body in [('kubectl',TRANSPORT),('ssh',TRANSPORT),('vllm',CLI)]:
            f=self.tmp/name
            f.write_text(body)
            f.chmod(0o755)
        (self.tmp/'sitecustomize.py').write_text(NETWORK)
        self.env={'PATH':str(self.tmp)+os.pathsep+os.environ['PATH'],'PYTHONPATH':str(self.tmp),
                  'TEST_READY':str(self.tmp/'server.pid'),'PYTHONDONTWRITEBYTECODE':'1'}
        self.cfg=worker.read(ROOT/'configs/targets.example.json')

    def execute(self,mode,kind):
        t=self.cfg['targets'][0 if kind=='k8s' else 1]
        t.update(served_name='test',vllm_bin=str(self.tmp/'vllm'),startup_timeout=8,interval=.1)
        t['connection'].update(python=sys.executable,tmp=str(self.tmp))
        t['settings']=t['settings'][:1]
        job=dict(action=mode,target=t,settings=t['settings'],scenarios=[
            dict(self.cfg['scenarios'][0],concurrency=[8],repeats=1)],seconds=.1)
        with patch.dict(os.environ,self.env),contextlib.redirect_stdout(io.StringIO()):
            # 빠른 확인을 위해 polling 대기만 줄인다. 실제 작업과 ZIP 전송 경로를 그대로 사용한다.
            import time
            sleep=time.sleep
            with patch.object(remote.time,'sleep',lambda x:sleep(min(x,.2))):
                rc=remote.start(job,self.tmp/'out')
        return rc,next((self.tmp/'out').glob('*/receipt.json'))

    def test_k8s_collect_and_retrieve(self):
        rc,receipt=self.execute('collect','k8s')
        self.assertEqual(rc,0)
        self.assertFalse((self.tmp/'server.pid').exists())
        self.assertTrue((receipt.parent/'collected/pod-events.log').is_file())
        with (receipt.parent/'runs.csv').open(encoding='utf-8-sig') as f:
            rows=list(csv.DictReader(f))
        self.assertEqual((len(rows),rows[0]['status'],rows[0]['quality']),(1,'ok','unknown'))
        data=summarize(next(receipt.parent.rglob('metrics.jsonl.gz')))
        self.assertGreaterEqual(data['ok'],2)
        self.assertEqual(data['series']['vllm:kv_cache_usage_perc{engine="0"}']['max'],.25)
        with patch.dict(os.environ,dict(self.env,TEST_POD_UID='changed')):
            with self.assertRaisesRegex(ValueError,'Pod UID'):
                remote.retrieve(receipt)

    def test_ssh_collect_failed_run(self):
        self.env['TEST_FAIL']='yes'
        rc,r=self.execute('collect','ssh')
        self.assertEqual(rc,1)
        row=worker.read(next(r.parent.rglob('html.json')))
        self.assertEqual(row['status'],'error')
        self.assertIsNone(row['p95_itl_ms'])

    def test_ssh_observe(self):
        rc,r=self.execute('observe','ssh')
        self.assertEqual(rc,0)
        self.assertFalse((r.parent/'runs.csv').exists())
        self.assertTrue(next(r.parent.rglob('metrics.jsonl.gz')).is_file())

    def test_managed_argv_and_cleanup(self):
        # 실제 포트 예약은 실장비 테스트에서 확인한다. 모의 환경에는 수신 소켓이 없다.
        with (self.tmp/'sitecustomize.py').open('a') as f:
            f.write("\nimport socket\nclass Dummy:\\n def __enter__(self):return self\\n def __exit__(self,*e):pass\\n def bind(self,a):pass\\nsocket.socket=Dummy\\n".replace("\\n","\n"))
        self.env['TEST_LAUNCH']='1'
        rc,r=self.execute('launch','ssh')
        self.assertEqual(rc,0)
        cmd=worker.read(next(r.parent.rglob('launch.json')))['argv']
        self.assertEqual(cmd[cmd.index('--max-num-seqs')+1],'64')
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.tmp/'server.pid').read_text()),0)

    def test_contract(self):
        m=worker.read(ROOT/'examples/meta.json')
        b=worker.read(ROOT/'examples/bench.json')
        self.assertEqual(worker.normalize(m,b)['data_kind'],'demo')
        for extra in ({'p95_itl_ms':None},{'p95_ttft_ms':0},{'max_concurrency':128},{'failed':1}):
            with self.assertRaises(ValueError):
                worker.normalize(m,dict(b,**extra))

    def test_counter_reset(self):
        z=self.tmp/'samples.gz'
        with gzip.open(z,'wt') as f:
            for i,n in enumerate((10,12,1,4)):
                f.write(json.dumps({'utc':i,'samples':[f'vllm:num_preemptions_total{{engine="0"}} {n}']})+'\n')
        s=next(iter(summarize(z)['series'].values()))
        self.assertEqual((s['observed_delta'],s['resets']),(6,1))


if __name__=='__main__':
    unittest.main()
