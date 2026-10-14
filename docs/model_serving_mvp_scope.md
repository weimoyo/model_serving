# Standalone Model Serving MVP scope

Status: implementation baseline
Date: 2026-10-13

The project is self-contained. Its local control plane owns node registration,
GPU inventory snapshots, placement, Docker runtime lifecycle, Model,
Deployment, Replica, readiness, catalog, and OpenAI-compatible inference.

Remote devices run only this project's authenticated Node Agent. No Snowpack
API, database, token, template, relay, or process is part of the runtime
dependency graph.

For the MVP, `desired_replicas` is exactly one. The database and reconciler use
a one-to-many Deployment/Replica structure so scaling can be added later.

A Replica is schedulable only when all are true:

1. its Node Agent is reachable and reports the node online;
2. its assigned physical GPU UUID is present;
3. its Docker container is running;
4. the SSH tunnel is alive and `/health` succeeds;
5. `/v1/models` contains the configured served-model identity.

Container failure retains history, removes the Replica from routing, deletes
the failed runtime, and creates a replacement to converge desired state.
