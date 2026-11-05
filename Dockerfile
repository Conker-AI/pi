FROM python:3.12-slim

WORKDIR /app

# DejaVu draws Cyrillic and Greek letters in exported PDFs (about 3 MB). The upgrade takes
# Debian security fixes newer than the base image.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pi ./pi
COPY gateway ./gateway

EXPOSE 8050
CMD ["python", "-m", "uvicorn", "pi.api.server:app", "--host", "0.0.0.0", "--port", "8050"]
