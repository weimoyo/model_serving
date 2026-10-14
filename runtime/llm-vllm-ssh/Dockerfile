# Standalone model-serving runtime: sshd is the externally visible contract;
# vLLM stays on loopback and is reached through an authenticated SSH tunnel.
# Override this with a pinned, ARM64-capable image for DGX Spark production.
ARG VLLM_BASE_IMAGE=nvcr.io/nvidia/vllm:25.09-py3
FROM ${VLLM_BASE_IMAGE}

USER root
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssh-server ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /workspace /var/run/sshd

COPY entrypoint.sh /usr/local/bin/model-serving-vllm-entrypoint
COPY healthcheck.py /usr/local/bin/vllm-healthcheck
RUN chmod 0755 /usr/local/bin/model-serving-vllm-entrypoint /usr/local/bin/vllm-healthcheck

ENV HOME=/workspace \
    VLLM_HOST=127.0.0.1 \
    VLLM_PORT=8000
WORKDIR /workspace
EXPOSE 22
HEALTHCHECK --interval=15s --timeout=5s --start-period=10m --retries=3 CMD ["/usr/local/bin/vllm-healthcheck"]
LABEL io.model-serving.runtime-contract="vllm-ssh-v1" \
      io.model-serving.runtime-role="llm-vllm-ssh"
ENTRYPOINT ["/usr/local/bin/model-serving-vllm-entrypoint"]
