# Efficient multi-stage build using official uv image
# Stage 1: Build dependencies with uv
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

# Set working directory
WORKDIR /app

# Enable bytecode compilation for faster startup
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Copy dependency files first (better caching)
COPY pyproject.toml uv.lock ./

# Install dependencies using uv sync (creates .venv automatically)
RUN uv sync --frozen --no-dev

# Copy source code
COPY src/ ./src/
COPY README.md ./

# The package is already installed via uv sync
# Fix shebangs to point to correct location after copy
RUN sed -i 's|#!/app/.venv/bin/python|#!/opt/venv/bin/python|' /app/.venv/bin/*

# Stage 2: Minimal runtime image
FROM python:3.12-slim

# Create non-root user
RUN useradd -m -u 1000 mcpuser

# Copy virtual environment from builder (uv creates .venv by default)
COPY --from=builder --chown=mcpuser:mcpuser /app/.venv /opt/venv
# Also copy the source code since it's referenced
COPY --from=builder --chown=mcpuser:mcpuser /app/src /opt/venv/lib/python3.12/site-packages/src

# Set environment
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Switch to non-root user
USER mcpuser
WORKDIR /home/mcpuser

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import psycopg2; print('healthy')" || exit 1

# Entry point
ENTRYPOINT ["mcp-server-aact"]
CMD []