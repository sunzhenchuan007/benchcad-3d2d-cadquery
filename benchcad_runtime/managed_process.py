"""Wait without model involvement; enforce deadline independently of heartbeat."""
import os
import signal
import subprocess
import time


def stop_tree(child):
    if child.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    else:
        # Callers on POSIX must use start_new_session=True.
        os.killpg(child.pid,signal.SIGKILL)
    child.wait(timeout=15)


def wait_managed(child, timeout, stop_files=(), heartbeat=None, log_path=None):
    start=time.monotonic()
    try:
        while child.poll() is None:
            if any(p.exists() for p in stop_files):
                stop_tree(child)
                return 'stopped'
            if time.monotonic()-start >= timeout:
                stop_tree(child)
                return 'time_limit'
            if heartbeat and log_path:
                # Refresh in memory; writing remains once per minute.
                with heartbeat.lock:
                    heartbeat.state['seconds_since_cli_log_update'] = (
                        round(time.time()-log_path.stat().st_mtime,1) if log_path.exists() else None)
            try:
                child.wait(timeout=min(.5,max(.001,timeout-(time.monotonic()-start))))
            except subprocess.TimeoutExpired:
                pass
        return 'finished' if child.returncode == 0 else 'failed'
    except BaseException:
        stop_tree(child)
        raise
