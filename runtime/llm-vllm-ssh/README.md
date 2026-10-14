# vLLM + SSH standalone runtime

This image keeps vLLM on `127.0.0.1:8000`. The project node agent publishes
only the container SSH port, and the local gateway reaches vLLM through a
key-authenticated tunnel. No Snowpack component is required.

Required environment variables are `MODEL_REPOSITORY`, immutable
`MODEL_REVISION`, and `SERVED_MODEL_NAME`. `VLLM_ARGS_JSON` is an optional JSON
object converted to vLLM CLI flags without shell evaluation.

For DGX Spark, build with a pinned ARM64/GB10-compatible base image and publish
the result by digest. Do not use a floating tag in a Deployment.
