FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Shanghai DATA_DIR=/data DISPLAY_DRIVER=mock
WORKDIR /app
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then cp /run/secrets/proxy_ca /usr/local/share/ca-certificates/cloud-build-proxy.crt; update-ca-certificates; fi \
    && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update -o APT::Update::Error-Mode=any && apt-get install -y --no-install-recommends \
    fonts-wqy-zenhei fonts-arphic-uming fonts-arphic-ukai fonts-noto-cjk \
    tzdata gcc swig libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-hardware.txt ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir -r requirements.txt -r requirements-hardware.txt \
    && rm -f /usr/local/share/ca-certificates/cloud-build-proxy.crt && update-ca-certificates \
    && apt-get purge -y --auto-remove gcc swig libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY app ./app
RUN mkdir /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import os,urllib.request,base64; r=urllib.request.Request('http://127.0.0.1:8000/api/health'); p=os.getenv('WEB_PASSWORD',''); r.add_header('Authorization','Basic '+base64.b64encode((os.getenv('WEB_USERNAME','admin')+':'+p).encode()).decode()); urllib.request.urlopen(r,timeout=4)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-graceful-shutdown", "300"]
