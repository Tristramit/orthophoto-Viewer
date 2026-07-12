# Ortho Viewer — web server image
#
# Builds on the official GDAL image (system libgdal + CLI tools), then
# installs the matching Python GDAL bindings and the FastAPI web stack.
# Desktop-only deps (PyQt6) are intentionally excluded — see requirements-web.txt.

FROM ghcr.io/osgeo/gdal:ubuntu-small-3.8.4

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-pip python3-dev build-essential libgdal-dev \
    && rm -rf /var/lib/apt/lists/*

ENV CPLUS_INCLUDE_PATH=/usr/include/gdal \
    C_INCLUDE_PATH=/usr/include/gdal

# Match the GDAL Python bindings to the system libgdal version.
RUN pip3 install --no-cache-dir "GDAL==$(gdal-config --version)"

WORKDIR /app
COPY requirements-web.txt .
RUN pip3 install --no-cache-dir -r requirements-web.txt

COPY viewer/ ./viewer/
COPY web_server.py .

ENV ORTHO_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8765

CMD ["python3", "web_server.py", "--host", "0.0.0.0", "--port", "8765"]
