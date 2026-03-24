FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install picon first (separate layer to bust cache on updates)
RUN pip install --no-cache-dir "picon @ git+https://github.com/willystumblr/picon.git@packaging"

# Install Python dependencies
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY api/main.py .

# Expose port
EXPOSE 8000

# Run with uvicorn
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
