FROM python:3.12-slim

WORKDIR /app

# DejaVu draws Cyrillic and Greek letters in exported PDFs (about 3 MB). The upgrade takes
# Debian security fixes newer than the base image. The base still ships libzstd1 without the
# CVE-2026-124851 fix (1.5.7+dfsg-1+deb13u1, trixie-security), so the build fails if the upgrade
# did not bring it in. Naming it also changes this layer, so a build cache from before the fix
# cannot be reused. Drop the check once python:3.12-slim ships the fixed version.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' libzstd1)" ge 1.5.7+dfsg-1+deb13u1 \
 && apt-get install -y --no-install-recommends fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pi ./pi
COPY gateway ./gateway

EXPOSE 8050
CMD ["python", "-m", "uvicorn", "pi.api.server:app", "--host", "0.0.0.0", "--port", "8050"]
