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
The reconstruction workspace receives only the common inspection/reconstruction
sections of that guide, excluding drawing-only templates, state and operation logs.
Main projections and genuine full/half sections may be used; half/offset helper
support is absent, so these require real raw geometry. No fake sections.

## 3. Loop: maximum 60 decision responses

Custom `OpenAIResponses` transport + existing `run_episode`; no CLI-as-agent.
Each response must be exactly one nonempty fenced `python` or `submit` block.
Protocol `ordered-python-blocks/2` tolerates surrounding prose, case differences,
and `py`. Multiple complete Python blocks execute in order as one program in one
process, consuming one decision and one execution budget. No block is silently
discarded. Mixed Python/submit, multiple submits, empty blocks, unknown fences,
and incomplete fences are rejected without execution. Original replies and the
parsed program are logged. A provider-marked incomplete response never executes.
Every decision, including formatting errors/refusals/failed code/submission,
consumes one round. Transport failures have a separate cap (default 3 total).
No model fallback. Fresh container/process each execution; files persist within
one stage. Feedback contains bounded stdout/stderr and changed PNGs at workspace
root or directly in `output/`. The next request embeds the actual PNG content,
not just a filename. Feedback reports the attachment count and observation rejections.
HTTP request bodies are capped at 64 MiB including history and encoded images;
`--max-request-bytes` can lower this to the provider's limit (DeepSeek: 48 MiB);
overflow stops before sending, without silently truncating history. Request
preparation time is deducted from the stage deadline. This is not a controller RAM quota.
Optional preview uses `render` or `Drawing.preview`. No automatic per-round CAD
render is required. Reaching the round/time limit without explicit submit fails.
Reconstruction has an optional `crop_image` helper for supplied images. There is
no built-in three-dimensional preview tool or mandatory multi-view rendering step.
Default crop output names are stable per source/region, preserving multiple crops
within one action. An explicitly reused output name still overwrites its image.
Every returned observation (including format/incomplete-response failures) states
remaining rounds/time and whether code executed or submission was rejected.
Successful execution is explicitly distinguished from task correctness. Failed
code may have changed workspace files; no rollback is implied. Truncated output
is marked and the agent is directed to query a smaller subset. The final three
remaining decisions include a submission reminder without forcing submission.
Request journals record context message/image counts and text length. Full
Visible text/image history remains in each request; no automatic reset or summarization was added.
Provider reasoning-state items are not currently replayed. This is distinct from
preserving visible history and differs from the local BenchCAD 2 reference harness.
The agent is instructed to keep editable code and short working notes in its
workspace rather than rewriting the full model every round.
Known incomplete-response reasons are retained and returned to the agent. Hitting
`max_output_tokens` is a response-budget event, not a Python/geometry failure.
For thinking models this cap includes reasoning; configure it explicitly before
a run and do not silently increase it during an episode.
`budget.max_consecutive_output_limits` (CLI `--max-consecutive-output-limits`)
defaults to 2. Two consecutive responses marked `max_output_tokens` stop the
episode as `response_budget_exhausted`. A different response resets this streak;
transport failures retain their separate retry cap. Both incomplete responses
still consume rounds and retain usage. The stop is recorded as `response_budget`,
not proof of bad geometry or a particular root cause. There is no automatic
token increase, model switch, or new episode. An unsubmitted artifact is unscored.

## 4. Output and submission

### Optional native tool interface

Use `--action-protocol native` (or `[api].action_protocol = "native"`) to select
`native-tools/1` explicitly. The default remains `fenced` for existing runs.
Every request supplies `execute_python(code: string)` and `submit({})` definitions.
Tool results carry their original `call_id`, with PNG content inside the tool result.
One model response still consumes one decision. Multiple calls in one response
are all rejected and each receives an error result; no subset is silently run.
Argument validation is enforced locally even if a provider ignores strict mode or
`parallel_tool_calls=false`. Native submit validates the saved artifact without an
extra Python execution. Known validation failures return feedback within the same
budget. The stage replaces legacy fenced-response instructions in its task copy.
Provider reasoning-state replay is not added by this interface change.

The fenced submission example below applies to the default interface only.

If the required artifact is already saved and ready, the exact final response is:

```submit
pass
```

The system prompt and final three remaining decisions include this example.
`pass` requests normal validation; it does not bypass checks. Printing
"submitting" in a `python` block remains ordinary execution and cannot submit.

Drawing requires `output/drawing.dxf`: clean ezdxf audit, nonempty geometry,
trusted worker render to PNG, then frozen DXF+PNG+hash manifest. Reconstruction
requires `output/model.step`: valid positive-volume solids, then frozen STEP+
manifest. Submit Python runs once per attempt. Failed submit code and recognized artifact
rejections return feedback for repair within the SAME remaining rounds/time.
Successful validation freezes the bundle; unknown infrastructure failures stop the stage. Files must be bounded, regular, single-link, non-symlink files
inside the expected workspace path. A copied submission is validated in a fresh
clean worker; it is never repaired. Finalized bundles are never overwritten.
Agent-authored working state is excluded from freeze/transfer and is not GT.

## 5. Resources

Default per stage: 7200 seconds, 60 decisions. Per code/validation invocation:
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
Expert-drawing baseline correction remains disabled. [Three-component scoring](METRICS.md)
adds expert preference and annotation collisions; missing evidence leaves total null.
With `--iou-vendor`, the controller automatically evaluates frozen
reconstruction in a separate Docker environment using the [normalized IoU adapter](../evaluation/README.md),
then merges the result into the summary and generates an offline report.
Without evaluation configuration the metric explicitly remains unscored. STEP validity is not
reconstruction correctness. Model HTTP requests share the remaining stage deadline.
Keep raw IoU and primitive baseline b (best box/sphere/cylinder). The corrected
value q=clamp((IoU-b)/(1-b),0,1) maps to reconstruction score: 0 at q<=0.90,
1 at q>=0.99, linearly interpolated between. For b=1, q is 1 only if raw IoU
is 1, otherwise 0. Final score multiplies this by expert preference and collision,
also in 0..1. This does not enable the deferred expert-drawing baseline.

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
# Budget updates and optional template table

Current default budgets are 60 decision responses and 7200 active seconds per
stage, with at most 500,000 output tokens per API response (including reasoning
tokens when reported by the provider). These are separate limits, not a 500,000
token budget for the whole run. Two consecutive incomplete output-limit
responses still stop the stage; budget increases are not automatic retries.

An explicitly authorized development continuation may extend a stage stopped
by its round or output-response budget. It preserves the transcript and files,
counts prior rounds, active time and usage cumulatively, and excludes paused
time. It writes a new run and records old/new budgets and source versions. It
must not be presented as an uninterrupted benchmark run. Historical runs keep
their original configuration and artifacts.

The optional product-specification table may be blank, filled, relabelled,
modified or removed by the drawing agent. Empty cells do not require invented
specifications. Template variants preserve the original template and are used
only for future drawing runs, not to change a submitted reconstruction input.
