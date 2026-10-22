# Case13: valve body / 阀体

This is the single public input example for the STEP-to-DXF task. It uses the
real Case13 model, not a generated demonstration shape.

这里提供真实 Case13 的输入 STEP；通用任务要求在 `benchcad_cadquery/tasks/`，
运行预算在根目录 `config.example.toml`。示例不包含专家 JSON、专家图纸、AI 答案或运行日志。

## Files

- `model.step`: drawing-stage input. Its STEP header was replaced with neutral
  publication metadata; the original DATA section is byte-for-byte unchanged.
- `manifest.json`: provenance, byte count and SHA-256 of the published input.
  This is documentation, not a CLI configuration file or a scoring answer.

## Run from the repository root

Complete [environment setup](../../docs/QUICKSTART.zh-CN.md) first. Set the API key
in the controller environment and use your exact supported multimodal model ID:

```sh
python3 tools/benchcad.py run \
  --config config.example.toml \
  --step examples/case13/model.step \
  --model YOUR_MODEL_ID \
  --run-dir .runs/case13-example-001 \
  --reconstruct
```

This command makes paid model requests. Set an explicit supported output-token
limit if your provider does not accept the default 500K per response. Each stage
has at most 60 decisions and 7200 active seconds. Use a new run directory each time.

Drawing receives this STEP. Reconstruction receives only the submitted drawing's
frozen DXF and PNG in a fresh workspace/context. Successful bundles and full logs
remain under `.runs/`; they are not uploaded automatically.

To enable reconstruction scoring, also supply `--iou-vendor` pointing to the
pinned evaluator checkout described in [evaluation setup](../../evaluation/README.md).
Without it the report explicitly says `evaluation_not_configured`. Expert
preference requires separately supplied expert data; this example does not
provide that data, so it does not by itself produce all three metric components.

The internal `smoke` fixture is a separate, fixed-code plumbing check. It is not
this example and does not test model capability.
