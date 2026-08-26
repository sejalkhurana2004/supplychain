# Optional -- Render can build directly from requirements.txt without this
# file at all (see README.md Step 2). Only needed if you prefer Docker-based
# deployment, or want to run this somewhere other than Render.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py main.py ./
COPY data ./data

ENV DATA_DIR=/app/data

CMD ["python", "main.py"]
