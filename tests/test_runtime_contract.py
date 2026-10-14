from pathlib import Path


def test_vllm_ssh_runtime_contract_is_loopback_only():
    root = Path(__file__).resolve().parents[1] / "runtime" / "llm-vllm-ssh"
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (root / "entrypoint.sh").read_text(encoding="utf-8")
    assert 'io.model-serving.runtime-contract="vllm-ssh-v1"' in dockerfile
    assert "EXPOSE 22" in dockerfile
    assert "EXPOSE 8000" not in dockerfile
    assert '--host "${VLLM_HOST:-127.0.0.1}"' in entrypoint
    assert "PermitOpen 127.0.0.1:${VLLM_PORT:-8000}" in entrypoint
    assert "wait -n" in entrypoint
