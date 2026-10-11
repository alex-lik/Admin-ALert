FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# зависимости отдельным слоем — кешируются между сборками
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY pyproject.toml ./
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations

RUN adduser --disabled-password --gecos "" app && chown -R app:app /srv
USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
