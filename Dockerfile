FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC \
    IMPERION_STORAGE_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 imperion

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY imperion ./imperion
COPY VERSION README.md ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN mkdir -p /data && chown -R imperion:imperion /data /app \
    && chmod 0755 /usr/local/bin/docker-entrypoint.sh


EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/health',timeout=4)" || exit 1

STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "imperion", "run"]
