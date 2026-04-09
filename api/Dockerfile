FROM python:3.11-slim

WORKDIR /app

# Limit OpenBLAS threads for constrained environments
ENV OPENBLAS_NUM_THREADS=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && uvicorn --version \
    && python -c "import fastapi; print('deps OK')"

# Copy application
COPY api/main.py .

# Copy static frontend (for local/Docker testing; not used on Railway)
COPY test-frontend/index.html static/index.html
COPY assets/ static/assets/

# Expose port
EXPOSE 8000

# Run with uvicorn — Railway sets $PORT dynamically
CMD ["sh", "-c", "echo \"Starting on port ${PORT:-8000}\" && exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
