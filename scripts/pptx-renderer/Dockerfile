FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-impress fonts-noto-cjk fonts-liberation fontconfig && rm -rf /var/lib/apt/lists/*
COPY worker.py /worker.py
ENV HOME=/tmp
USER 1000:1000
CMD ["python", "/worker.py"]
