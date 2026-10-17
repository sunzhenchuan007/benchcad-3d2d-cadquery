# Server pilot agent specification

## 1. Inputs

Drawing: `input/model.step`, generic TASK/TOOLS. Reconstruction: only frozen
`input/drawing.dxf` + `input/drawing.png`, generic TASK/TOOLS. The initial vision
message uses the controller-owned frozen PNG. Separate contexts/workspaces;
no source STEP, drawing code/state/log, GT, score, or expert reference transfers.

## 2. Tools

Linux Python 3.11, CadQuery/OCP, ezdxf, PyMuPDF, Pillow, NumPy/SciPy. Generic
read-only toolkit source; raw CadQuery/OCP/ezdxf allowed. `Drawing.projection`,
`full_section`, native linear dimensions, raw ezdxf dimensions, and `render` are
documented in the self-contained [TOOLS](../benchcad_cadquery/TOOLS.md).
Main projections and genuine full/half sections may be used; half/offset helper
support is absent, so these require real raw geometry. No fake sections.

## 3. Loop: maximum 30 decision responses

Custom `OpenAIResponses` transport + existing `run_episode`; no CLI-as-agent.
Each response must be exactly one nonempty fenced `python` or `submit` block.
Every decision, including formatting errors/refusals/failed code/submission,
consumes one round. Transport failures have a separate cap (default 3 total).
No model fallback. Fresh container/process each execution; files persist within
one stage. Feedback contains bounded stdout/stderr and changed PNGs at workspace
root or directly in `output/`. The next request embeds the actual PNG content,
not just a filename. Feedback reports the attachment count and observation rejections.
HTTP request bodies are capped at 64 MiB including history and encoded images;
overflow stops before sending, without silently truncating history. Request
preparation time is deducted from the stage deadline. This is not a controller RAM quota.
Optional preview uses `render` or `Drawing.preview`. No automatic per-round CAD
render is required. Reaching the round/time limit without explicit submit fails.

## 4. Output and submission

Drawing requires `output/drawing.dxf`: clean ezdxf audit, nonempty geometry,
trusted worker render to PNG, then frozen DXF+PNG+hash manifest. Reconstruction
requires `output/model.step`: valid positive-volume solids, then frozen STEP+
manifest. Submit Python runs once; finalization failure ends the stage without
model repair. Files must be bounded, regular, single-link, non-symlink files
inside the expected workspace path. A copied submission is validated in a fresh
clean worker; it is never repaired. Finalized bundles are never overwritten.
Agent-authored working state is excluded from freeze/transfer and is not GT.

## 5. Resources

Default per stage: 1800 seconds, 30 decisions. Per code/validation invocation:
300 seconds or remaining stage time, 1 CPU, 4096 MiB memory with equal swap
limit (no additional swap), 128 PIDs. Non-root UID/GID, read-only root, no network,
ALL capabilities dropped, no-new-privileges, 256 MiB noexec/nosuid/nodev /tmp.
Only workspace RW and generic package RO are bound; no keys, Docker socket,
home directory, or GT are mounted.
stdout+stderr disk writes capped at 8 MiB per Docker invocation; overflow stops
the owned container. Total execution deadline is supervised by the controller,
not merely a Docker-client timeout. Forced removal is confirmed before reading
artifacts; cleanup uses bounded calls and may exceed the execution budget by
about 20 seconds. Cleanup failure fails closed. Persistent workspace disk quota
and controller crash/daemon-outage reconciliation are operator responsibilities.

## 6. Records

Controller-owned episode messages, replies, events, usage, code snapshots,
bounded stdout/stderr, execution timing/limits/container name, validated PNG
snapshots named by SHA-256, and frozen manifest hashes. PNG: at most 12 MiB,
4096 pixels per dimension, 16 million pixels, one frame, up to 32 changed/invalid
entries per execution; both locations share a 4096-entry scan cap. Prior observations
are never read back from writable agent paths. Agent statements, source-state
metadata, and image contents remain untrusted; hashes establish byte identity,
not geometric truth. Records stay outside all agent mounts and are local-only.

## 7. Metrics

Primary accounting: `local_visible_text` with pinned tiktoken 0.12.0/o200k_base,
an unverified model-tokenizer proxy. Recounts visible instructions/history on
each request. Image, hidden reasoning/instruction, framing, caching, and total
billed tokens are unknown. Provider usage is diagnostic only; actual fee null.
Section/collision interfaces are not scored. Expert baseline disabled. No fake
IoU or overall score; the operator independently evaluates frozen reconstruction
with the [normalized IoU adapter](../evaluation/README.md). STEP validity is not
reconstruction correctness. Model HTTP requests share the remaining stage deadline.
Keep raw IoU and primitive baseline b (best box/sphere/cylinder). The corrected
value q=clamp((IoU-b)/(1-b),0,1) maps to reconstruction score: 0 at q<=0.90,
100 at q>=0.99, linearly interpolated between. For b=1, q is 1 only if raw IoU
is 1, otherwise 0. This is not the deferred expert-drawing baseline or an overall score.

## 8. Status

Development pilot, not an engineering-complete toolkit or proven hostile-code
sandbox. Default feedback tests mock Docker and make no model API calls.
On Linux, setting `BENCHCAD_TEST_DOCKER_IMAGE` enables a real Docker feedback
loop check, still without a model API. Optional template tests require ezdxf.
Server operator must build the image, run doctor/smoke, validate real filesystem
deny probes, and run API episodes. Complex CAD geometry, section fidelity,
annotation quality, logging truth, and model success are not established by
the trusted plate replay or these unit tests. The legacy Windows process runner
is not used by the Linux Docker pilot.
