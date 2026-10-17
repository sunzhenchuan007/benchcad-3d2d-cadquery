from pathlib import Path
import json
import os
import sys
import tempfile
import unittest

from benchcad_runtime.process import run_process


@unittest.skipUnless(os.name == "nt", "Windows Job acceptance; Linux sandbox is not claimed")
class ProcessLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_script(self, source, **kwargs):
        return run_process([sys.executable, "-c", source], self.root, self.root/"logs",
                           kwargs.pop("timeout", 8), os.environ.copy(), **kwargs)

    def test_worker_has_one_cpu_affinity(self):
        result = self.run_script("import win32api,win32process; print(win32process.GetProcessAffinityMask(win32api.GetCurrentProcess())[0].bit_count())")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"].strip(), "1")
        self.assertFalse(result["resource_limits"]["filesystem_isolation"])

    def test_timeout_kills_worker_before_late_file_write(self):
        result = self.run_script("import time,pathlib; time.sleep(4); pathlib.Path('late.txt').write_text('bad')", timeout=0.6)
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["exit_code"], 124)
        self.assertFalse((self.root/"late.txt").exists())

    def test_output_limit_is_not_reported_as_timeout(self):
        result = self.run_script("import sys,time; sys.stdout.write('x'*100000); sys.stdout.flush(); time.sleep(4)", log_bytes=1024)
        self.assertEqual(result["stop_reason"], "log_limit")
        self.assertFalse(result["timed_out"])

    def test_job_memory_limit_is_applied(self):
        code = ("import win32job; "
                "print(win32job.QueryInformationJobObject(None,win32job.JobObjectExtendedLimitInformation)['JobMemoryLimit'])")
        result = self.run_script(code, memory_mib=256)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(int(result["stdout"].strip()), 256*1024*1024)


if __name__ == "__main__":
    unittest.main()
