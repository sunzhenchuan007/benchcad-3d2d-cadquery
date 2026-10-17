# BenchCAD 3D2D CadQuery

A minimal **development pilot**: a custom Responses API agent draws a STEP in
Linux Docker; an optional independent agent reconstructs from the frozen DXF and
PNG. The tested agent is an API model, not Codex CLI. Windows is for development
and offline unit tests only. Expert baseline and section/collision scoring
are disabled; a separate [normalized IoU evaluator](evaluation/README.md) is provided.
No claim of an engineering-complete drawing toolkit or
verified hostile-code isolation. See [agent contract](docs/AGENT_SPEC.md) and
[Chinese setup](README.zh-CN.md).

For the setup/check/run wrapper see [quick start](docs/QUICKSTART.zh-CN.md).
For retained requests, replies, execution errors and immutable image snapshots
see [logging](docs/LOGGING.md). [Operation JSON](docs/OPERATION_LOG.md) is an
unverified declaration converter; controlled capture and section scoring remain pending.

## Linux setup

Use Python >=3.11 and a non-root account able to access a **local** Docker daemon
at its standard Unix socket. Docker workers run with the controller's non-root
UID/GID so bind-mount outputs are writable. The controller needs only Pillow and
tiktoken beyond the standard library. It keeps API credentials; workers receive
neither keys nor the daemon socket. No home, GT, or whole-repository mount.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[local-accounting]'
docker build -t benchcad-cadquery:local sandbox
export TIKTOKEN_CACHE_DIR="$PWD/.runs/tokenizer-cache"
.venv/bin/python tools/provision_tokenizer.py --cache-dir "$TIKTOKEN_CACHE_DIR"
.venv/bin/python -m benchcad_cadquery.cli doctor --run-dir .runs/doctor-001 --image benchcad-cadquery:local
.venv/bin/python -m benchcad_cadquery.cli smoke --run-dir .runs/smoke-001 --image benchcad-cadquery:local
```

Install/build/provision may use the network. Episodes load a checksum-verified
offline tokenizer; they never download vocabulary. The image also caches that
vocabulary at build time. Image build context is only `sandbox/`. Direct
dependencies are pinned there; transitive dependencies and base-image digest
are not a reproducibility lock. Keep the resulting image ID with deployment records.
The controller does not read Docker login configuration or select remote contexts.

`doctor` checks the offline host tokenizer, imports worker dependencies, and
exercises a limited container launch/removal. `smoke` replays a simple parametric
plate with three projections, a real full section, native dimensions, drawing
validation/render/freeze, and separate STEP reconstruction. It is **trusted
replay, not a model benchmark**; reconstruction uses known fixture dimensions.
Both require Docker and create a new exclusive run directory. Geometry and
filesystem-denial acceptance probes must be run by the server operator.

## API run (operator only)

Set `OPENAI_API_KEY` in the controller environment using your deployment's secret
handling. Set `BASE_URL` (or `OPENAI_BASE_URL`) for a compatible Responses endpoint;
HTTPS is required except loopback HTTP. No login files or automatic local config
are consulted. The operator must supply the exact model ID; there is no fallback.

```sh
.venv/bin/python -m benchcad_cadquery.cli run \
  --step /data/input/model.step --run-dir .runs/run-001 \
  --model gpt-6-sol --image benchcad-cadquery:local --reconstruct \
  --rounds 30 --wall-seconds 1800 --execution-seconds 300 --memory-mib 4096 --cpus 1
```

Omit `--reconstruct` for drawing only. `--config config.example.toml` loads only
that explicitly selected file; CLI options take priority. Set `api.model` or
pass `--model`. An explicit configured base URL takes precedence over environment
aliases. Lower decision/time budgets are allowed, higher than 30/1800/300 are not.
Each stage gets a fresh context, workspace, container sequence, and budget.

Results: `summary.json`, per-stage `episode/`, `executions/`, `finalize/`, and
`bundle/manifest.json`. Only successful explicit submission creates a bundle;
validation has no model repair loop. A bundle contains DXF+PNG or STEP plus its
manifest. Working state stays untrusted in the original workspace. Reconstruction
receives only copies of the frozen DXF+PNG and generic TASK/TOOLS documents; its
initial image references the frozen PNG. Records and code/image snapshots are
not mounted. Never reuse a run directory or modify a finalized bundle.

Main usage metric: **local_visible_text**, an unverified tokenizer proxy for
visible input/output text. It excludes image, hidden reasoning, hidden
instruction, framing, and billing tokens. Provider usage is diagnostic only;
`actual_fee` and all unimplemented scores stay null. Evaluate the frozen
reconstructed STEP independently with `evaluation/normalized_iou.py` and the
external pinned geometry modules described in [evaluation setup](evaluation/README.md).
The API subprocess deadline and HTTP timeout use the remaining stage budget.

## Local checks and limitations

```sh
python -m unittest discover -s tests -v
```

Default feedback tests use mocks and local files, without model API requests.
Set `BENCHCAD_TEST_DOCKER_IMAGE` on Linux to exercise the real Docker feedback loop.
Optional template tests require ezdxf. Existing Windows process-limit tests are preserved. Docker
worker geometry, actual filesystem deny probes, and deployment behavior are
not established by unit tests. Main-view projection/full-section helpers are
limited; half/offset sections require genuine raw OCP construction. No fake
sections or automatic engineering completeness checks are supplied.

CPU, memory, swap, PID, temporary-storage, stdout/stderr, and execution-time
limits are configured. The controller forcibly removes only its own uniquely
named container after every invocation, including timeout/overflow. Cleanup
adds a bounded grace period (up to about 20 seconds of Docker cleanup calls)
beyond the execution deadline; cleanup failure stops the episode before artifact
reads. A daemon outage/controller crash still requires operator reconciliation.
Persistent workspace disk usage has **no enforced quota** in this pilot: deploy
on a dedicated quota-limited filesystem, and apply operator retention limits.
Docker shares the host kernel and is not a proof of adversarial isolation.
Parser/audit success and immutable controller snapshots are useful records,
not proof that arbitrary model statements or geometry are correct.

`cases/index.json` is provenance only; source CAD and private GT are not bundled.
Run artifacts are local-only and must not be committed or published wholesale.
