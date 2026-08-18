ARG BASE_IMAGE=pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/alphafactor-build
COPY pyproject.toml README.md ./
COPY factor_service ./factor_service
RUN python -m pip install --no-cache-dir . \
    && rm -rf /opt/alphafactor-build

WORKDIR /workspace
CMD ["python", "-m", "factor_service.research.remote_runner", "--help"]
