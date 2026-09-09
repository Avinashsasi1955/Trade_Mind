FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN DEBIAN_FRONTEND=noninteractive apt-get update && apt-get install -y --no-install-recommends postgresql-client curl ca-certificates \
    && curl --fail --silent --show-error https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem -o /etc/ssl/certs/rds-global-bundle.pem \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN sed '/^torch==/d' requirements.txt > /tmp/requirements-container.txt \
    && pip install --no-cache-dir torch==2.12.1 --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r /tmp/requirements-container.txt \
    && useradd --create-home --uid 10001 nivesh
COPY . .
RUN chown -R nivesh:nivesh /app
USER nivesh
EXPOSE 4173
CMD ["uvicorn","backend.asgi:app","--host","0.0.0.0","--port","4173","--workers","2","--no-server-header","--no-proxy-headers"]
