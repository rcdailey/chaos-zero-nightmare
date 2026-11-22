# One image for both workloads: `czn-data daily` (CronJob) and `czn-data serve` (Deployment).
FROM ghcr.io/astral-sh/uv:0.13-python3.14-trixie-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable
# The search models (embedding and reranker) ship in the image; HF_HUB_OFFLINE below keeps them
# from refetching.
ENV FASTEMBED_CACHE_PATH=/app/models
RUN .venv/bin/python -c "from czn_data import embed, rerank; \
    embed.passages(['warm up']); rerank.scores('warm', ['up'])"
# So do the screenshot identity models, pinned to the revision the daily job computes prints with.
ENV CZN_IDENTITY_DIR=/app/identity
RUN .venv/bin/python -c "from czn_data import identity; identity.download('/app/identity')"

FROM python:3.14-slim-trixie
RUN useradd --system --uid 1000 app
COPY --from=build /app/.venv /app/.venv
# Owned by the app user: the Hugging Face cache keeps some files private to their owner. The
# cluster runs the pods as UID 1000, so the image uses that UID too.
COPY --from=build --chown=1000 /app/models /app/models
COPY --from=build /app/identity /app/identity
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 FASTEMBED_CACHE_PATH=/app/models \
    HF_HUB_OFFLINE=1 CZN_IDENTITY_DIR=/app/identity
USER 1000
EXPOSE 8000
ENTRYPOINT ["czn-data"]
CMD ["serve"]
