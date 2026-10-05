FROM python:3.14-slim AS base
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONPATH=/app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY core/ core/
COPY pm/ pm/
COPY migrations/ migrations/
COPY web/ web/

FROM base AS test
COPY requirements-dev.txt pyproject.toml ./
RUN pip install -r requirements-dev.txt
COPY tests/ tests/
CMD ["pytest"]

FROM base
RUN useradd -r app
USER app
