FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

RUN pip install "celery[redis]>=5.4"
COPY celery_app.py ./
COPY tasks ./tasks
COPY providers ./providers

RUN useradd --system --uid 10001 --no-create-home appuser
USER appuser

CMD ["celery", "-A", "celery_app", "worker", "--loglevel=info", "--concurrency=2"]
