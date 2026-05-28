# Use an official Python image as the base
FROM python:3.12-slim

# Set the working directory inside the container
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy the project files into the container
COPY . /app

# Install Python dependencies. claude-code extra pulls in claude-agent-sdk
# AND opentelemetry-api (SDK injects W3C trace context; missing OTEL emits
# noisy DEBUG tracebacks each call).
RUN pip install --root-user-action=ignore -e ".[claude-code]"

# Expose the project directory as a volume
VOLUME ["/app"]

# Set the entry point to the openevolve-run.py script
ENTRYPOINT ["python", "/app/openevolve-run.py"]