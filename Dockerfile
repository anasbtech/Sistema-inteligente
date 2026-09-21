FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Dependências nativas do OpenCV e do onnxruntime
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libgomp1 ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt requirements-min.txt ./
# CPU por padrão; para GPU troque onnxruntime-gpu e instale o torch com CUDA.
RUN sed -i 's/onnxruntime-gpu/onnxruntime/' requirements.txt \
    && pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY frontend ./frontend
COPY worker ./worker
COPY scripts ./scripts
COPY sql ./sql
COPY run.py ./

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
    CMD curl -fsS http://localhost:8000/saude || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
