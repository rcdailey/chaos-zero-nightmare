# One image for both workloads: `czn-data scrape` (CronJob) and `czn-data serve` (Deployment).
FROM ghcr.io/astral-sh/uv:0.13-python3.14-trixie-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable
# The search embedding model ships in the image; HF_HUB_OFFLINE below keeps it from refetching.
ENV FASTEMBED_CACHE_PATH=/app/models
RUN .venv/bin/python -c "from czn_data import embed; embed.passages(['warm up'])"

FROM python:3.14-slim-trixie
RUN useradd --system --uid 10001 app
COPY --from=build /app/.venv /app/.venv
# Owned by the app user: the Hugging Face cache keeps some files private to their owner.
COPY --from=build --chown=10001 /app/models /app/models
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 FASTEMBED_CACHE_PATH=/app/models \
    HF_HUB_OFFLINE=1
USER 10001
EXPOSE 8000
ENTRYPOINT ["czn-data"]
CMD ["serve"]
