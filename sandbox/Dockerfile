FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglu1-mesa libegl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
    libx11-6 libgomp1 fontconfig fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Build context is sandbox/ ONLY. No repository, cases, or credentials are copied.
COPY requirements.txt /opt/requirements.txt
RUN pip install --no-cache-dir -r /opt/requirements.txt \
    && pip check \
    && mkdir -p /opt/tiktoken-cache \
    && python -c "import tiktoken; tiktoken.get_encoding('o200k_base')" \
    && chmod -R a+rX /opt/tiktoken-cache \
    && useradd --uid 10001 --create-home worker

USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/python"]
