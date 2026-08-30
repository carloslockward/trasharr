FROM python:3.12-alpine

WORKDIR /app

# OCI Annotations
LABEL org.opencontainers.image.source = "https://github.com/carloslockward/trasharr"

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application into the image
COPY . .

# Config is mounted at runtime via volume; document the default path.
ENV TRASHARR_CONFIG=/config/config.json
RUN mkdir -p /config

EXPOSE 5000

# By default, run the gunicorn production server.
ENTRYPOINT ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "2", "--timeout", "120", "run:app"]