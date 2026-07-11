FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-cloud.txt .
RUN pip install --no-cache-dir -r requirements-cloud.txt

COPY app.py transcriber.py guide.py ./
COPY static/ static/

# Cloud deployments transcribe via Groq (GROQ_API_KEY), so faster-whisper
# is intentionally NOT installed — keeps the image small and fast to build.
EXPOSE 8000
CMD ["python", "app.py"]
