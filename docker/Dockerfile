# Backend image for the EdLab demo (Daejeon 26 server).
# The repository is mounted at runtime (see docker-compose-backend.yml); this image only carries dependencies.
FROM python:3.11-slim

WORKDIR /workspace/PICon_Demo

# Limit OpenBLAS threads for constrained environments
ENV OPENBLAS_NUM_THREADS=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    git \
    build-essential \
    curl \
    vim \
    lsof \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && uvicorn --version \
    && python -c "import fastapi, picon; print('deps OK')"
RUN rm requirements.txt
