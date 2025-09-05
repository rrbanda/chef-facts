# Dockerfile (optional but handy)
```dockerfile
FROM python:3.11-slim

# system deps
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY extractor.py batch_runner.py ./

# default to a shell; users will pass commands as needed
ENTRYPOINT ["/bin/bash"]