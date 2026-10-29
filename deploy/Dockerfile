# Optional disposable acceptance fixtures, not an application installation path.
FROM python:3.13-slim-bookworm AS source
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/home/broker \
    UV_PYTHON_DOWNLOADS=never PATH="/app/.venv/bin:$PATH"
RUN groupadd --gid 10001 broker \
    && useradd --uid 10001 --gid 10001 --create-home broker \
    && install -d -o broker -g broker /app \
    && install -d -o broker -g broker -m 0700 /data /data/artifacts /run/demo /run/worker /worker-state /results
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ ./src/
COPY contracts/ ./contracts/
COPY tools/container_smoke.py /opt/container_smoke.py
COPY deploy/control-entrypoint.sh /opt/control-entrypoint.sh
RUN uv sync --locked --no-dev --no-editable --no-cache

FROM source AS control
USER 10001:10001
EXPOSE 8765
ENTRYPOINT ["/bin/sh", "/opt/control-entrypoint.sh"]
CMD []

FROM source AS worker
USER root
RUN apt-get update \
    && apt-get install --yes --no-install-recommends openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*
USER 10001:10001
CMD ["python", "-m", "compute_relay.worker"]

FROM control AS smoke
ENTRYPOINT ["python", "/opt/container_smoke.py"]
