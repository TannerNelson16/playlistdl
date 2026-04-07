
# Use a base image with Python
FROM python:3.12-alpine
ENV PYTHONUNBUFFERED=1

# Install dependencies
RUN apk add --no-cache ca-certificates curl python3-dev git ffmpeg

# Set up a virtual environment for dependencies
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python packages
RUN pip install --no-cache-dir flask spotdl

# Create directories
WORKDIR /app
RUN mkdir -p /app/downloads

# Copy application code
COPY . /app
# COPY web /app/web

# Expose the application port
EXPOSE 5000

# Run the application
CMD ["python3", "/app/main.py"]


