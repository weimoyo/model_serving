#!/usr/bin/env python3
import json
import os
import sys
import urllib.request


port = int(os.getenv("VLLM_PORT", "8000"))
served = os.environ["SERVED_MODEL_NAME"]
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
        if response.status >= 400:
            raise RuntimeError(f"health returned {response.status}")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=3) as response:
        models = json.load(response)
    if served not in {item.get("id") for item in models.get("data", [])}:
        raise RuntimeError(f"served model {served!r} missing")
except Exception as exc:
    print(exc, file=sys.stderr)
    sys.exit(1)
