FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
ENV PORT=8080
CMD ["sh", "-c", "uv run --no-dev uvicorn --factory speckeeper.main:app --host 0.0.0.0 --port ${PORT}"]
