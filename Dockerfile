FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    DEBIAN_FRONTEND=noninteractive \
    NODE_BIN=/usr/local/bin/node

# 国内源加速（N100 直连海外的镜像是慢/不通的）
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl xz-utils tzdata \
 && rm -rf /var/lib/apt/lists/*

# node 22：解 YouTube 的 n 签名（yt-dlp-ejs 需要 JS 运行时）
ARG NODE_VER=22.22.2
RUN curl -fsSL "https://registry.npmmirror.com/-/binary/node/v${NODE_VER}/node-v${NODE_VER}-linux-x64.tar.xz" \
      -o /tmp/node.tar.xz \
 && mkdir -p /opt/node \
 && tar -xJf /tmp/node.tar.xz -C /opt/node --strip-components=1 \
 && rm /tmp/node.tar.xz \
 && ln -sf /opt/node/bin/node /usr/local/bin/node \
 && ln -sf /opt/node/bin/npm /usr/local/bin/npm \
 && node -v

RUN pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
      "fastapi==0.115.6" "uvicorn[standard]==0.34.0" "python-multipart==0.0.20" \
      "yt-dlp==2026.8.19" "yt-dlp-ejs==0.8.0" "bgutil-ytdlp-pot-provider==2.0.2"

WORKDIR /app
COPY server.py /app/server.py
COPY static /app/static
RUN mkdir -p /data/downloads

EXPOSE 8300
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD curl -fsS http://127.0.0.1:8300/healthz || exit 1

CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8300", "--no-access-log"]
