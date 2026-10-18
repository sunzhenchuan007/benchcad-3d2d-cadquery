"""Linux setup/check/run entrypoint. Never installs Docker or requests an API key."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / '.venv/bin/python'
IMAGE = 'benchcad-cadquery:local'


def invoke(argv, env=None):
    subprocess.run([str(x) for x in argv], cwd=ROOT, env=env, check=True)


def preparation_environment():
    """No model credentials in Docker, pip, vocabulary provisioning or replay."""
    names = ('PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'TMPDIR',
             'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY',
             'http_proxy', 'https_proxy', 'no_proxy',
             'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE')
    return {name: os.environ[name] for name in names if name in os.environ}


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ('-h', '--help'):
        print('Usage: python3 tools/benchcad.py setup | check | run [runtime arguments]')
        print('Requires Linux, Python >=3.11, and a working local Docker daemon.')
        print('setup: prepare project venv, image and vocabulary; check: doctor + fixed replay;')
        print('run: forward arguments to the API runner; credentials stay in environment.')
        return 0
    if not sys.platform.startswith('linux') or sys.version_info < (3, 11):
        raise RuntimeError('Use Linux with Python >=3.11; no Windows/WSL installation is performed')
    if os.getuid() == 0:
        raise RuntimeError('Use a non-root account with local Docker access')
    action, extra = args[0], args[1:]
    if action not in ('setup', 'check', 'run'):
        raise ValueError('Choose setup, check, or run')
    if action != 'run' and extra:
        raise ValueError('setup/check do not accept extra arguments')
    # A fresh empty Docker config avoids reading login files or remote contexts.
    # All preparation/check subprocesses receive a restricted environment.
    clean_env = preparation_environment()
    with tempfile.TemporaryDirectory(prefix='benchcad-docker-') as config:
        docker = ['docker', '--config', config, '--host', 'unix:///var/run/docker.sock']
        invoke([*docker, 'info', '--format', '{{.ServerVersion}}'], clean_env)
        return perform(action, extra, docker, clean_env)


def perform(action, extra, docker, clean_env):
    env = dict(os.environ if action == 'run' else clean_env,
               TIKTOKEN_CACHE_DIR=str(ROOT / '.runs/tokenizer-cache'))
    if action == 'setup':
        if not PYTHON.exists():
            invoke([sys.executable, '-m', 'venv', ROOT / '.venv'], env)
        invoke([PYTHON, '-m', 'pip', 'install', '-e', '.[local-accounting]'], env)
        invoke([PYTHON, 'tools/provision_tokenizer.py', '--cache-dir', env['TIKTOKEN_CACHE_DIR']], env)
        invoke([*docker, 'build', '-t', IMAGE, 'sandbox'], env)
        invoke([*docker, 'build', '--build-arg', 'CAD_IMAGE=' + IMAGE,
                '-t', 'benchcad-cadquery:evaluator', 'evaluation'], env)
        print('Environment prepared. Next: python3 tools/benchcad.py check')
        return 0
    if not PYTHON.exists():
        raise RuntimeError('Run setup first')
    if action == 'run':
        invoke([PYTHON, '-m', 'benchcad_cadquery.cli', 'run', *extra], env)
    else:
        from datetime import datetime, timezone
        import uuid
        label = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
        for command in ('doctor', 'smoke'):
            invoke([PYTHON, '-m', 'benchcad_cadquery.cli', command, '--image', IMAGE,
                    '--run-dir', ROOT / '.runs' / (command + '-' + label)], env)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'Command failed ({type(exc).__name__}); review the preceding step. Existing runs are retained.', file=sys.stderr)
        raise SystemExit(1)
