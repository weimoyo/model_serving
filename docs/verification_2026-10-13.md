# Verification report — 2026-10-13

The standalone implementation is verified with an actual HTTP mock runtime and
an in-process Node Runtime double. The test covers node inventory, physical GPU
UUID placement, runtime creation, readiness, catalog publication,
non-streaming chat, SSE ending in `[DONE]`, container failure removal,
replacement, and Deployment deletion.

A separate process-level test started three Uvicorn processes: the OpenAI
runtime double, this project's Node Agent double, and the real local gateway.
The real gateway called the Node Agent over HTTP, allocated `GPU-MOCK-0001`,
created a Replica, passed both readiness probes, published the catalog, and
completed both JSON and SSE chat requests. The stream returned HTTP 200 and
ended with `data: [DONE]`.

`pytest -q`: 4 passed. Python compilation and the runtime entrypoint's
`bash -n` check also passed.

The laboratory addresses accept SSH connections, but this execution context
does not possess a login credential, so installation of the standalone Node
Agent and real-model discovery remain pending. No credential is stored in this
repository.
