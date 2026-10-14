from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path
    api_key: str | None
    gateway_ssh_private_key: Path
    ssh_known_hosts_file: Path
    node_secret_dir: Path | None = None
    reconcile_interval_seconds: float = 5.0
    node_timeout_seconds: float = 5.0
    inference_timeout_seconds: float = 300.0
    run_reconciler: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(__file__).resolve().parent.parent
        state = Path(os.getenv("MODEL_SERVING_STATE_DIR", str(root / ".state")))
        api_key_file = Path(os.getenv("MODEL_SERVING_API_KEY_FILE", str(state / "control.token")))
        api_key = os.getenv("MODEL_SERVING_API_KEY")
        if not api_key and api_key_file.exists():
            api_key = api_key_file.read_text(encoding="utf-8").strip() or None
        return cls(
            database_path=Path(os.getenv("MODEL_SERVING_DB", str(state / "model_serving.sqlite3"))),
            api_key=api_key,
            gateway_ssh_private_key=Path(os.getenv("MODEL_SERVING_SSH_KEY", str(state / "gateway_ed25519"))),
            ssh_known_hosts_file=Path(os.getenv("MODEL_SERVING_KNOWN_HOSTS", str(state / "known_hosts"))),
            node_secret_dir=Path(os.getenv("MODEL_SERVING_NODE_SECRET_DIR", str(state / "node-secrets"))),
            reconcile_interval_seconds=float(os.getenv("MODEL_SERVING_RECONCILE_SECONDS", "5")),
            node_timeout_seconds=float(os.getenv("MODEL_SERVING_NODE_TIMEOUT", "5")),
            inference_timeout_seconds=float(os.getenv("MODEL_SERVING_INFERENCE_TIMEOUT", "300")),
            run_reconciler=os.getenv("MODEL_SERVING_RECONCILER", "1").lower() not in {"0", "false"},
        )
