# AutoDL research image

Build the image on the AutoDL node from an AlphaFactorService checkout:

```bash
docker build -f docker/research/Dockerfile -t alphafactor-research:latest .
```

The server transfers the current `factor_service` Python package into each
isolated run and mounts it read-only over the image copy. The image therefore
provides the pinned CUDA/Python dependencies, while every job runs the same
application source as the dispatching AlphaFactorService instance.

The remote container receives only the immutable dataset snapshot, the frozen
job descriptor, and source code. It does not receive ClickHouse or PostgreSQL
credentials and never calls back to the control service.
