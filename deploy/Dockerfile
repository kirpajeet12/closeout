FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY closeout closeout
COPY web web
ENV CLOSEOUT_DATA_DIR=/data
EXPOSE 8765
CMD ["python", "-m", "uvicorn", "closeout.api:app", "--host", "0.0.0.0", "--port", "8765", "--proxy-headers", "--forwarded-allow-ips", "*"]
