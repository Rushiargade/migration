FROM python:3.12-slim

WORKDIR /app

# System dependencies for Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Copy package metadata
COPY pyproject.toml ./

# Install build dependencies
RUN pip install --no-cache-dir build

# Copy application source code
COPY vmigrate ./vmigrate
COPY config ./config

# Install package with all dependencies including web UI
RUN pip install --no-cache-dir -e ".[web]"

# Create directories for runtime artifacts
RUN mkdir -p /tmp/vmigrate /app/logs /app/data

# Expose ports
EXPOSE 8080

# Health check for the web UI
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import socket; s=socket.socket(); s.connect(('localhost', 8080)); s.close()" || exit 1

# Default: launch web UI
CMD ["vmigrate", "serve", "--host", "0.0.0.0", "--port", "8080"]
