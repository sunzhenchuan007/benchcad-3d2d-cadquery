import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from benchcad_runtime.heartbeat import Heartbeat
from benchcad_runtime.managed_process import wait_managed
from benchcad_runtime.view_summary import summarize, generate


class AutomationTests(unittest.TestCase):
    def test_view_scope_and_stale_state(self):
        dxf=b'0\nSECTION\n2\nENTITIES\n0\nINSERT\n2\nVIEW_TOP\n0\nENDSEC\n0\nEOF\n'
        state={'dxf_sha256':hashlib.sha256(dxf).hexdigest(),'views':[{'id':'TOP','block':'VIEW_TOP','type':'projection','normal':[0,0,1],'xdir':[1,0,0]}]}
        self.assertEqual(summarize(state,dxf)['views'][0]['operation'],'custom_projection')
        self.assertEqual(summarize(state,dxf,["d.projection('TOP')"])['views'][0]['operation'],'orthographic_projection')
        self.assertEqual(summarize(state,dxf,["f=d.model.Faces()[74]\nd._project(f,'TOP')"])['views'][0]['operation'],'partial_projection')
        with self.assertRaises(ValueError):summarize(state,dxf+b'changed')

    def test_missing_state_not_empty_success(self):
        with tempfile.TemporaryDirectory() as folder:
            result=generate(Path(folder))
            self.assertEqual(result['status'],'unavailable')
            self.assertEqual(json.loads((Path(folder)/'view-operations.json').read_text())['status'],'unavailable')

    def test_heartbeat_immediate_transition_periodic_and_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'heartbeat.json'
            with self.assertRaises(RuntimeError):
                with Heartbeat(path,interval=.05,stage='drawing') as hb:
                    first=json.loads(path.read_text())['updated_at']
                    time.sleep(.13)
                    self.assertNotEqual(first,json.loads(path.read_text())['updated_at'])
                    hb.update(stage='reconstruction')
                    self.assertEqual(json.loads(path.read_text())['stage'],'reconstruction')
                    raise RuntimeError('fixture')
            self.assertEqual(json.loads(path.read_text())['status'],'failed')
            self.assertFalse(hb.thread.is_alive())
            self.assertEqual(Heartbeat(path).interval,60)

    def test_timeout_and_stop_do_not_wait_for_heartbeat(self):
        with tempfile.TemporaryDirectory() as folder:
            for stop in (False,True):
                path=Path(folder)/'STOP'
                if stop:path.touch()
                options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
                child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'],**options)
                started=time.monotonic()
                status=wait_managed(child,.15,stop_files=[path])
                self.assertEqual(status,'stopped' if stop else 'time_limit')
                self.assertIsNotNone(child.poll())
                self.assertLess(time.monotonic()-started,5)

    def test_detached_launcher_returns_before_worker_finishes(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            done=folder/'done.txt'
            command=[sys.executable,str(root/'tools/launch_background.py'),'--log-dir',str(folder/'logs'),'--',sys.executable,'-c',
                     'import time;from pathlib import Path;time.sleep(1);Path('+repr(str(done))+').write_text("done")']
            result=subprocess.run(command,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertFalse(done.exists())
            deadline=time.monotonic()+5
            while not done.exists() and time.monotonic()<deadline:time.sleep(.05)
            self.assertEqual(done.read_text(),'done')


if __name__=='__main__':unittest.main()
