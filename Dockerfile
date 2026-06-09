FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    XDG_CONFIG_HOME=/data

COPY pyproject.toml uv.lock README.md ./
COPY praison ./praison
RUN uv sync --frozen --no-dev

EXPOSE 24601

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD ["uv", "run", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:24601/health')"]

CMD ["uv", "run", "--no-sync", "python", "-m", "praison"]
