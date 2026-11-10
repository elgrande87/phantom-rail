FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    DOWNLOAD_FOLDER=/downloads \
    TMPDIR=/downloads/.tmp_work

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /downloads/.tmp_work && chmod 1777 /downloads/.tmp_work
EXPOSE 5000
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "--timeout", "3600", "app:app"]
