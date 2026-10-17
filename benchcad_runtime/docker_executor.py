"""Linux Docker development pilot. No Docker SDK, credentials, or host execution.

The daemon is trusted. This is not a certified hostile-code sandbox. Each call
creates and removes its own container; cleanup failure is infrastructure failure.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from .artifacts import MAX_PNG_BYTES, read_regular, sha256, snapshot_png, write_new
from .episode import Execution, clipped


@dataclass(frozen=True)
class DockerLimits:
    memory_mib: int = 4096
    cpus: float = 1
    pids: int = 128
    log_bytes: int = 8 * 1024 * 1024  # combined stdout + stderr, per invocation

    def __post_init__(self):
        for name in ('memory_mib', 'pids', 'log_bytes'):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError('Memory, PID, and log limits must be positive integers')
        if type(self.cpus) not in (int, float) or not math.isfinite(self.cpus) or self.cpus <= 0:
            raise ValueError('CPU limit must be finite and positive')


class DockerExecutor:
    def __init__(self, workspace: Path, records: Path, source: Path,
                 image: str, limits: DockerLimits = DockerLimits(),
                 uid: int | None = None, gid: int | None = None):
        self.workspace = Path(workspace).resolve(strict=True)
        self.records = Path(records).resolve()
        self.source = Path(source).resolve(strict=True)
        if any(a.is_relative_to(b) or b.is_relative_to(a) for a, b in (
                (self.workspace, self.records), (self.workspace, self.source),
                (self.source, self.records))):
            raise ValueError('Workspace, source, and records must be disjoint')
        if not image or image.startswith('-') or any(c.isspace() for c in image):
            raise ValueError('A local Docker image name is required')
        self.uid = uid if uid is not None else (os.getuid() if hasattr(os, 'getuid') else 1000)
        self.gid = gid if gid is not None else (os.getgid() if hasattr(os, 'getgid') else 1000)
        if self.uid <= 0 or self.gid < 0:
            raise ValueError('Run the controller as a non-root Linux user')
        self.image, self.limits = image, limits
        self.records.mkdir(parents=True, exist_ok=False)
        self.config = self.records / 'docker-config'
        self.config.mkdir()
        self.seen_images: dict[str, str] = {}

    def docker(self) -> list[str]:
        # Explicit empty config prevents reading the operator's Docker login file.
        # Explicit local socket ignores remote Docker contexts and DOCKER_HOST.
        return ['docker', '--config', str(self.config), '--host', 'unix:///var/run/docker.sock']

    @staticmethod
    def environment() -> dict[str, str]:
        return {'PATH': '/usr/local/bin:/usr/bin:/bin' if sys.platform.startswith('linux') else os.defpath,
                'LANG': 'C.UTF-8'}

    def create_command(self, name: str, command: list[str]) -> list[str]:
        for path in (self.workspace, self.source):
            if ',' in str(path) or '\n' in str(path):
                raise ValueError('Docker mount paths cannot contain commas/newlines')
        return [*self.docker(), 'create', '--name', name, '--pull', 'never',
                '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges', '--pids-limit', str(self.limits.pids),
                '--memory', f'{self.limits.memory_mib}m', '--memory-swap', f'{self.limits.memory_mib}m',
                '--cpus', str(self.limits.cpus), '--user', f'{self.uid}:{self.gid}',
                '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777',
                '--log-driver', 'none', '--init', '--workdir', '/workspace',
                '--mount', f'type=bind,src={self.workspace},dst=/workspace',
                '--mount', f'type=bind,src={self.source},dst=/opt/benchcad,readonly',
                '--env', 'HOME=/tmp', '--env', 'PYTHONPATH=/opt/benchcad',
                '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'PYTHONUNBUFFERED=1',
                '--env', 'OMP_NUM_THREADS=1', '--env', 'OPENBLAS_NUM_THREADS=1',
                '--entrypoint', '/usr/local/bin/python', self.image, *command]

    def _process(self, argv: list[str], directory: Path, deadline: float) -> tuple[int, str | None]:
        """Drain pipes continuously into strictly bounded disk logs, never PIPE communicate()."""
        if time.monotonic() >= deadline:
            return 124, 'timeout'
        lock, overflow = threading.Lock(), threading.Event()
        used = [0]
        errors: list[Exception] = []
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=self.environment(), start_new_session=True)

        def drain(pipe, filename):
            try:
                with (directory / filename).open('xb') as output:
                    while True:
                        chunk = pipe.read(8192)
                        if not chunk:
                            break
                        with lock:
                            keep = min(len(chunk), self.limits.log_bytes - used[0])
                            used[0] += keep
                        output.write(chunk[:keep])
                        if keep < len(chunk):
                            overflow.set()
            except Exception as exc:
                errors.append(exc)
                overflow.set()
            finally:
                pipe.close()

        threads = [threading.Thread(target=drain, args=(pipe, filename), daemon=True)
                   for pipe, filename in ((proc.stdout, 'stdout.txt'), (proc.stderr, 'stderr.txt'))]
        for thread in threads:
            thread.start()
        stop = None
        try:
            while proc.poll() is None or any(t.is_alive() for t in threads):
                if overflow.is_set():
                    stop = 'log_limit'
                elif time.monotonic() >= deadline:
                    stop = 'timeout'
                if stop:
                    proc.kill()
                    break
                time.sleep(0.01)
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=5)
        if any(t.is_alive() for t in threads) or errors:
            raise RuntimeError('Docker log capture failed')
        if overflow.is_set():
            stop = 'log_limit'
        return (124 if stop == 'timeout' else 125 if stop else proc.returncode), stop

    def _remove_owned(self, name: str):
        # Called after killing/waiting the client, including create timeout. Two
        # attempts accommodate a transient daemon operation; no broad container cleanup.
        for _ in range(2):
            try:
                removed = subprocess.run([*self.docker(), 'rm', '--force', name],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, env=self.environment(), timeout=5)
                checked = subprocess.run([*self.docker(), 'container', 'inspect', name],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, env=self.environment(), timeout=5)
                if removed.returncode == 0 and checked.returncode == 1:
                    return
            except (OSError, subprocess.TimeoutExpired):
                pass
        raise RuntimeError('Could not confirm removal of owned container: ' + name)

    def run_command(self, command: list[str], timeout: float, directory: Path) -> Execution:
        if not sys.platform.startswith('linux'):
            raise RuntimeError('Docker execution requires a Linux host; Windows is development only')
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('Execution timeout must be finite and positive')
        directory.mkdir(parents=True, exist_ok=False)
        deadline = time.monotonic() + timeout
        name = 'benchcad-' + uuid.uuid4().hex
        metadata = {'container': name, 'image': self.image, 'command': command, 'limits': vars(self.limits)}
        (directory / 'execution.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
        started = time.monotonic()
        code, stop = 125, None
        created = False
        try:
            create = directory / 'create'
            create.mkdir()
            code, stop = self._process(self.create_command(name, command), create, deadline)
            if not code:
                created = True
                attached = directory / 'attached'
                attached.mkdir()
                code, stop = self._process([*self.docker(), 'start', '--attach', name], attached, deadline)
            else:
                attached = create
        finally:
            # Never take artifacts while a worker might still be running.
            metadata.update(exit_code=code, stop_reason=stop, elapsed_seconds=time.monotonic()-started,
                            cleanup_confirmed=False)
            try:
                self._remove_owned(name)
            except BaseException as exc:
                metadata['cleanup_error_type'] = type(exc).__name__
                (directory / 'execution.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
                raise
        metadata.update(exit_code=code, stop_reason=stop, elapsed_seconds=time.monotonic()-started,
                        cleanup_confirmed=True)
        (directory / 'execution.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
        if not created and stop is None:
            raise RuntimeError('Docker container creation failed; see execution records')
        def feedback(filename):
            p = attached / filename
            if not p.exists():
                return ''
            with p.open('rb') as stream:
                data = stream.read(10000)
                if p.stat().st_size > 10000:
                    stream.seek(max(0, p.stat().st_size - 10000))
                    data += b'\n[output truncated]\n' + stream.read(10000)
            return clipped(data.decode('utf-8', 'replace'))
        return Execution(code, feedback('stdout.txt'), feedback('stderr.txt') +
                         (f'\nStopped: {stop}' if stop else ''), timed_out=stop == 'timeout')

    def observe(self, deadline: float | None = None) -> tuple[list[str], list[dict]]:
        images, records = [], []
        # Do not recursively walk an agent-controlled directory tree.
        with os.scandir(self.workspace) as entries:
            for count, entry in enumerate(entries):
                if deadline is not None and time.monotonic() >= deadline:
                    records.append({'rejected': 'observation deadline'})
                    break
                if count >= 4096:
                    records.append({'rejected': 'top-level entry scan limit'})
                    break
                if not entry.name.lower().endswith('.png'):
                    continue
                if len(records) >= 32:
                    records.append({'rejected': 'PNG observation count limit'})
                    break
                try:
                    data = read_regular(self.workspace, entry.name, MAX_PNG_BYTES)
                    digest = sha256(data)
                    if self.seen_images.get(entry.name) == digest:
                        continue
                    path = snapshot_png(data, self.records / 'observations')
                    self.seen_images[entry.name] = digest
                    images.append(str(path))
                    records.append({'name': entry.name, 'sha256': digest, 'snapshot': path.name})
                except (OSError, ValueError, SyntaxError) as exc:
                    records.append({'name': entry.name, 'rejected': type(exc).__name__})
        return images, records

    def execute(self, code: str, timeout: float) -> Execution:
        deadline = time.monotonic() + timeout
        execution_id = uuid.uuid4().hex
        # The canonical code is outside the workspace. The worker executes a
        # per-call workspace copy; agent changes to that copy are not trusted logs.
        write_new(self.records / (execution_id + '.py'), code.encode('utf-8'), readonly=True)
        script = '_execution_' + execution_id + '.py'
        write_new(self.workspace / script, code.encode('utf-8'))
        result = self.run_command(['-B', '-P', '/workspace/' + script], max(.001, deadline-time.monotonic()),
                                  self.records / execution_id)
        result.images, observations = self.observe(deadline)
        (self.records / execution_id / 'observations.json').write_text(
            json.dumps(observations, indent=2), encoding='utf-8')
        return result
