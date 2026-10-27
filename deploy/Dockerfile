# One image, three entry points: `lira serve` (API), `lira worker` (reviews), and the intake Lambda
# (entrypoint python -m awslambdaric, cmd lira.aws.ingest.handler). Models and the spaCy pipeline are baked in.
FROM python:3.12-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir --upgrade pip
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install . awslambdaric \
 && pip install --no-cache-dir --prefix=/install https://github.com/explosion/spacy-models/releases/download/en_core_web_md-3.8.0/en_core_web_md-3.8.0-py3-none-any.whl

FROM python:3.12-slim
RUN useradd --uid 10001 --create-home lira
COPY --from=build /install /usr/local
WORKDIR /app
COPY config ./config
COPY models ./models
COPY data/world ./data/world
COPY reports ./reports
ENV LIRA_ROOT=/app PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')" || exit 1
CMD ["lira", "serve", "--port", "8080"]
