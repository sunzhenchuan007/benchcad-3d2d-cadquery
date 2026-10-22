"""Local controller liveness only: no model calls, no remote polling."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time


class Heartbeat:
    def __init__(self, path, interval=60, **state):
        if interval <= 0:
            raise ValueError('interval must be positive')
        self.path = Path(path)
        self.interval = interval
        self.state = {'status':'running', **state}
        self.started = time.monotonic()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None

    def _write(self):
        with self.lock:
            row = {'controller_pid':os.getpid(), 'updated_at':datetime.now(timezone.utc).isoformat(),
                   'elapsed_seconds':round(time.monotonic()-self.started,1),
                   'interval_seconds':self.interval, **self.state}
            self.path.parent.mkdir(parents=True,exist_ok=True)
            temp = self.path.with_suffix(self.path.suffix+'.tmp')
            temp.write_text(json.dumps(row,indent=2),encoding='utf-8')
            temp.replace(self.path)

    def update(self, **state):
        with self.lock:
            self.state.update(state)
            self._write()

    def _loop(self):
        while not self.stop_event.wait(self.interval):
            self._write()

    def __enter__(self):
        self._write()
        self.thread = threading.Thread(target=self._loop,daemon=True)
        self.thread.start()
        return self

    def __exit__(self, kind, exc, tb):
        self.stop_event.set()
        self.thread.join()
        self.update(status='stopped' if kind and issubclass(kind,InterruptedError) else 'failed' if kind else self.state.get('terminal_status','finished'),
                    error_type=kind.__name__ if kind else None)
