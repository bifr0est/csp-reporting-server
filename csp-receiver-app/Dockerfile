FROM ghcr.io/astral-sh/uv:python3.13-alpine

WORKDIR /app

# Copy uv project files for reproducible builds
COPY pyproject.toml uv.lock ./

# Install dependencies using lockfile (ensures exact same versions)
RUN uv sync --frozen --no-dev

COPY app.py .

# Use Gunicorn for a more production-like WSGI server
# Exposing on port 5000, which docker-compose will map or Nginx will connect to.
# Increase workers based on your load and CPU cores available to the container.
CMD ["uv", "run", "gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "app:app"]