# Framework update

This release contains framework code, tests, documentation and example settings.
It adds no case data, expert references, CAD artifacts, credentials or raw run
logs. The existing source index is unchanged.

- Default budgets: 60 model decision responses and 7200 active seconds per stage;
  500000 requested output tokens per API response, including reasoning.
- CAD execution remains limited to 300 seconds per call, 1 CPU and 4096 MiB.
  Drawing and reconstruction have independent contexts and stage budgets.
- Native `execute_python` / `submit` tools are available through explicit opt-in.
  Tool results preserve call correlation and changed PNG feedback. The fenced
  reply protocol remains available and is still the default configuration.
- Explicit development continuation retains transcripts, workspace files,
  cumulative time, rounds and usage; original run records remain separate.
- Paper-layout DXF templates render through the same import path used for
  drawing. Optional product tables may be filled, modified or removed.
- Drawing operations can be converted into verified view evidence for expert
  preference scoring. Unsupported evidence and missing expert data remain
  unscored rather than receiving invented values.
- Reconstruction, expert preference and annotation collision each use a 0..1
  scale; their product is computed only when all required scores are available.
- Reports, progress records and curated display exports preserve independent
  execution, scoring and publication states. Display caches replay the frozen
  scoring alignment without changing scores or calling a model.

The 500K request ceiling has not been validated against every provider. A
provider may reject it or enforce a lower model-specific maximum. The framework
does not silently clamp the setting, change models or initiate another paid run.
Configure an explicit supported override when needed. These settings are not
retrospectively applied to historical trials.

Public code includes only the framework side of optional workbench integration;
the receiving website and deployment-specific service configuration are external.
Tests that require that separate checkout explicitly skip when it is absent.
