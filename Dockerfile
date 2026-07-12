FROM python:3.12-slim

# ffmpeg: audio extraction | tesseract: OCR for photographed slides/boards
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# OPTIONAL: uncomment to accept legacy .ppt/.doc/.xls uploads directly
# (adds ~400 MB to the image; otherwise users get a clear "save as modern
# format" message for legacy files)
RUN apt-get update && apt-get install -y --no-install-recommends \
   libreoffice --no-install-recommends && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-cloud.txt .
RUN pip install --no-cache-dir -r requirements-cloud.txt

COPY *.py ./
COPY static/ static/

# Cloud deployments transcribe via Groq (GROQ_API_KEY), so faster-whisper
# is intentionally NOT installed — keeps the image small and fast to build.
EXPOSE 8000
CMD ["python", "app.py"]
