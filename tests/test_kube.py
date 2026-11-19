"""PC에서 실제 자식 프로세스인 모의 kubectl을 구동해 관측·정리·오류 동작을 확인한다."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import remote
import kube_observe as kube

STUB = """#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
d = Path(os.environ['KUBE_TEST'])
a = sys.argv[1:]
with (d/'calls').open('a') as f:
    print(json.dumps(a),file=f)
if a == ['config','current-context']:
    print('demo-context')
elif 'get' in a:
    pod = json.loads((d/'pod').read_text())
    if (d/'replace').exists() and (d/'already').exists(): pod['metadata']['uid']='another'
    (d/'already').touch()
    print(json.dumps(pod))
elif 'port-forward' in a:
    if (d/'deny').exists(): sys.exit('Forbidden: pods/portforward')
    (d/'pid').write_text(str(os.getpid()))
    print('Forwarding from 127.0.0.1:43210 -> 8000', flush=True)
    time.sleep(60)
elif 'logs' in a:
    if (d/'no-logs').exists(): sys.exit('Forbidden: pods/log')
    print('2026-01-01T00:00:00Z Cuda out of memory')
else:
    sys.exit('unexpected command')
"""


@unittest.skipIf(os.name == "nt", "모의 kubectl은 POSIX 실행 파일을 사용한다")
class ObservePod(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.d = Path(temp.name)
        binary = self.d / "kubectl"
        binary.write_text(STUB)
        binary.chmod(0o755)
        self.env = patch.dict(os.environ, {"KUBE_TEST": str(self.d),
                              "PATH": str(self.d) + os.pathsep + os.environ["PATH"]})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.pod = {"spec": {"containers": [{"name": "vllm"}]},
                    "metadata": {"uid": "pod1"},
                    "status": {"containerStatuses": [{"name": "vllm", "containerID": "c1", "restartCount": 0}]}}
        self.out = self.d / "results"

    def invoke(self, *flags, payloads=()):
        (self.d / "pod").write_text(json.dumps(self.pod))
        argv = ["remote.py", "kube", "-n", "demo", "--pod", "llm-1", "--seconds", "1",
                "--interval", "5", "--out", str(self.out), *flags]
        with patch.object(sys, "argv", argv), patch.object(kube, "local_metrics", side_effect=payloads) as read, \
                patch.object(kube.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            rc = remote.main()
        path, = self.out.glob("*/kubectl-summary.json")
        calls = [json.loads(s) for s in (self.d/"calls").read_text().splitlines()]
        return rc, json.loads(path.read_text()), calls, read

    def test_sample_identity_metrics_and_cleanup(self):
        fmt = 'vllm:num_requests_waiting{{engine="0"}} {}\nvllm:generation_tokens_total{{engine="0"}} {}\n'
        rc, r, calls, read = self.invoke(payloads=[fmt.format(12,100), fmt.format(0,103)])
        self.assertEqual((rc, r["status"], r["logs"]["oom_lines"]), (0, "ok", 1))
        self.assertEqual(read.call_count, 2)
        self.assertEqual(read.call_args.args, ("http://127.0.0.1:43210/metrics",))
        series = r["metrics"]["series"]
        self.assertEqual(series['vllm:generation_tokens_total{engine="0"}']['observed_delta'], 3)
        self.assertEqual(series['vllm:num_requests_waiting{engine="0"}']['max'], 12)
        self.assertTrue(r["identity_verified"])
        self.assertEqual(calls[0], ["config","current-context"])
        self.assertTrue(all(c[:4] == ["--context","demo-context","--namespace","demo"] for c in calls[1:]))
        self.assertTrue(any("127.0.0.1" in c and ":8000" in c for c in calls))
        self.assertFalse(any("exec" in c for c in calls))
        self.assertTrue(any("--since-time="+r["start_utc"] in c for c in calls))
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.d/"pid").read_text()), 0)

    def test_logs_only_accepts_context_and_skips_tunnel(self):
        rc, r, calls, read = self.invoke("--logs-only", "--context", "explicit")
        self.assertEqual((rc, r["metrics"], r["logs"]["lines"]), (0, None, 1))
        read.assert_not_called()
        self.assertFalse(any("port-forward" in c or "current-context" in c for c in calls))

    def test_denied_tunnel_preserves_logs(self):
        (self.d/"deny").touch()
        rc, r, _, read = self.invoke()
        read.assert_not_called()
        self.assertEqual((rc, r["status"], r["metrics"]), (1, "partial", None))
        self.assertIn("Forbidden", r["warnings"][0])
        self.assertIsNotNone(r["logs"])

    def test_failed_http_and_logs_exclude_run(self):
        (self.d/"no-logs").touch()
        rc, r, _, _ = self.invoke(payloads=[ValueError("HTTP 403"), ValueError("HTTP 403")])
        self.assertEqual((rc, r["status"], r["metrics"]["failed"]), (1, "invalid", 2))
        self.assertTrue(any("403" in w for w in r["warnings"]))

    def test_replacement_excludes_all_evidence(self):
        (self.d/"replace").touch()
        rc, r, _, _ = self.invoke("--logs-only")
        self.assertEqual((rc, r["identity_verified"], r["logs"], r["status"]), (1, False, None, "invalid"))

    def test_ambiguous_container_lists_choices_before_start(self):
        self.pod["spec"]["containers"].append({"name":"sidecar"})
        with self.assertRaisesRegex(ValueError, "vllm, sidecar"):
            self.invoke()
        self.assertEqual(kube.choose_container(self.pod, "vllm"), "vllm")
        self.assertFalse(self.out.exists())
