FROM ghcr.io/astral-sh/uv:python3.13-alpine

WORKDIR /app

# Copy uv project files for reproducible builds
COPY pyproject.toml uv.lock ./

# Install dependencies using lockfile (ensures exact same versions)
RUN uv sync --frozen --no-dev

COPY worker.py .

CMD ["uv", "run", "python", "worker.py"]