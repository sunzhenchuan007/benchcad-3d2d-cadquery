"""Bound trusted diagnostic processes. Resource control is not file isolation."""
from pathlib import Path
import json
import math
import os
import signal
import subprocess
import sys
import time
import uuid


class WindowsJob:
    def __init__(self, memory_mib, cpu_count):
        import win32job
        import win32api
        import win32process
        self.api = win32job
        self.name = "BenchCAD-CadQuery-" + uuid.uuid4().hex
        self.handle = win32job.CreateJobObject(None, self.name)
        try:
            info = win32job.QueryInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation)
            mask, _ = win32process.GetProcessAffinityMask(win32api.GetCurrentProcess())
            allowed = [1 << i for i in range(64) if mask & (1 << i)]
            if not 0 < cpu_count <= len(allowed):
                raise ValueError("Requested CPU count exceeds available affinity")
            info["BasicLimitInformation"]["LimitFlags"] = (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | win32job.JOB_OBJECT_LIMIT_JOB_MEMORY
                | win32job.JOB_OBJECT_LIMIT_AFFINITY)
            info["BasicLimitInformation"]["Affinity"] = sum(allowed[:cpu_count])
            info["JobMemoryLimit"] = int(memory_mib * 1024 * 1024)
            win32job.SetInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation, info)
        except BaseException:
            self.handle.Close()
            raise

    def assign(self, pid):
        import win32api, win32con
        handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pid)
        try:
            self.api.AssignProcessToJobObject(self.handle, handle)
        finally:
            handle.Close()

    def kill(self):
        self.api.TerminateJobObject(self.handle, 124)

    def close(self):
        self.handle.Close()


class WindowsProcess:
    """Create suspended, retaining the thread handle that Popen would discard.

    Only duplicated standard handles are inherited. The caller must assign the
    process to its Job before resume(), including when argv[0] is a venv launcher.
    """

    def __init__(self, argv, cwd, env, out, err):
        import _winapi
        import msvcrt
        self.api = _winapi
        self.handle = self.thread_handle = None
        self.returncode = None
        inherited = []
        try:
            with open(os.devnull, "rb") as stdin:
                current = _winapi.GetCurrentProcess()
                for stream in (stdin, out, err):
                    inherited.append(_winapi.DuplicateHandle(
                        current, msvcrt.get_osfhandle(stream.fileno()), current,
                        0, True, _winapi.DUPLICATE_SAME_ACCESS))
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESTDHANDLES
                startup.hStdInput, startup.hStdOutput, startup.hStdError = inherited
                startup.lpAttributeList = {"handle_list": inherited}
                self.handle, self.thread_handle, self.pid, _ = _winapi.CreateProcess(
                    None, subprocess.list2cmdline(argv), None, None, True,
                    0x00000004 | subprocess.CREATE_NO_WINDOW,  # CREATE_SUSPENDED
                    env, os.fspath(cwd), startup)
        finally:
            for handle in inherited:
                _winapi.CloseHandle(handle)

    def resume(self):
        import win32process
        # The documented API returns the previous suspend count; exactly one
        # suspension is owned here. Any other result is a launch failure.
        if win32process.ResumeThread(self.thread_handle) != 1:
            raise OSError("Unexpected primary thread suspend count")
        self.api.CloseHandle(self.thread_handle)
        self.thread_handle = None

    def poll(self):
        if self.returncode is None and self.api.WaitForSingleObject(self.handle, 0) == self.api.WAIT_OBJECT_0:
            self.returncode = self.api.GetExitCodeProcess(self.handle)
        return self.returncode

    def wait(self, timeout):
        if self.api.WaitForSingleObject(self.handle, math.ceil(timeout * 1000)) == self.api.WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(str(self.pid), timeout)
        return self.poll()

    def kill(self):
        if self.poll() is None:
            self.api.TerminateProcess(self.handle, 124)

    def close(self):
        try:
            self.kill()
            self.wait(timeout=10)
        finally:
            for name in ("thread_handle", "handle"):
                handle = getattr(self, name)
                if handle is not None:
                    self.api.CloseHandle(handle)
                    setattr(self, name, None)


def run_process(argv, cwd, logs, timeout, env, memory_mib=4096, cpu_count=1, log_bytes=8*1024*1024):
    if (any(isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0 for value in (timeout, memory_mib))
            or int(memory_mib * 1024 * 1024) < 1
            or any(type(value) is not int or value <= 0 for value in (cpu_count, log_bytes))):
        raise ValueError("Finite positive resource limits and integer CPU/log limits required")
    logs = Path(logs)
    logs.mkdir(parents=True, exist_ok=False)
    job = WindowsJob(memory_mib, cpu_count) if os.name == "nt" else None
    start = time.monotonic()
    proc = None
    stop = None
    try:
        with (logs / "stdout.txt").open("wb") as out, (logs / "stderr.txt").open("wb") as err:
            if job:
                proc = WindowsProcess(argv, cwd, env, out, err)
                job.assign(proc.pid)
                proc.resume()
            else:
                proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--gate", json.dumps(argv)],
                                        cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                        start_new_session=True)
                proc.stdin.write(b"go\n")
                proc.stdin.close()
            while proc.poll() is None:
                if time.monotonic() - start >= timeout:
                    stop = "timeout"
                elif sum((logs / f).stat().st_size for f in ("stdout.txt", "stderr.txt")) > log_bytes:
                    stop = "log_limit"
                if stop:
                    if job:
                        job.kill()
                    else:
                        os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=10)
                    break
                time.sleep(0.05)
        code = proc.returncode if not stop else 124
        return {"exit_code": code, "timed_out": stop == "timeout", "stop_reason": stop,
                "elapsed_seconds": time.monotonic() - start,
                "resource_limits": {"memory_mib": memory_mib if job else None,
                                    "cpu_affinity_count": cpu_count if job else None,
                                    "filesystem_isolation": False},
                "stdout": read_tail(logs / "stdout.txt"), "stderr": read_tail(logs / "stderr.txt")}
    finally:
        try:
            if job:
                job.close()
        finally:
            if isinstance(proc, WindowsProcess):
                # Also terminates an unassigned, still-suspended process when
                # assignment fails. Always release both native handles.
                proc.close()
            elif proc is not None and proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=10)


def read_tail(path, size=20000):
    with Path(path).open("rb") as stream:
        length = stream.seek(0, 2)
        stream.seek(max(0, length-size))
        return stream.read(size).decode("utf-8", "replace")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--gate" or sys.stdin.readline().strip() != "go":
        raise SystemExit(2)
    raise SystemExit(subprocess.call(json.loads(sys.argv[2])))
