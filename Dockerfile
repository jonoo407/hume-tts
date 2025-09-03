FROM python:3.11-slim

# Install system dependencies including ffmpeg for audio conversion
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy server code
COPY server.py .

# Make server executable
RUN chmod +x server.py

# Create non-root user and switch
RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

# Expose the default port
EXPOSE 10201

# Simple TCP healthcheck against the Wyoming port
HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD python3 -c 'import socket,sys; s=socket.create_connection(("127.0.0.1",10201),2); s.close()'

# Set default command
ENTRYPOINT ["python3", "server.py"]
