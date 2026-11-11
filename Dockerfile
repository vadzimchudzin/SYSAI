FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libpango-1.0-0 libpangoft2-1.0-0 \
      libfontconfig1 fonts-dejavu-core fonts-noto-core && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY hub/seed hub/seed
COPY samples samples
COPY agents/sync agents/sync
COPY agents/installer agents/installer
COPY agents/desktop agents/desktop
# One thread for numpy's BLAS: the networks of the speech detector and of the speaker labelling run on one core, and
# idle BLAS threads only burn the CPU of a small instance.
ENV DATA_DIR=/srv/data PYTHONUNBUFFERED=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
