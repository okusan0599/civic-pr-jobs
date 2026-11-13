FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY web ./web
COPY data/seed_jobs.json ./data/seed_jobs.json
RUN useradd --create-home appuser && mkdir -p /var/data && chown appuser:appuser /var/data
ENV DATA_DIR=/var/data SCHEDULER_ENABLED=true CRAWL_ON_START=true PYTHONUNBUFFERED=1
USER appuser
EXPOSE 8000
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--workers","1"]
