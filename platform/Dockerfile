FROM python:3.12-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Python deps
COPY platform/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all platform packages into /app/ (flat)
# This means: /app/ingestion/, /app/alarm_engine/, /app/api/, /app/simulator/
COPY platform/ingestion/ ./ingestion/
COPY platform/alarm_engine/ ./alarm_engine/
COPY platform/api/ ./api/
COPY platform/simulator/ ./simulator/

# Default command (overridden per service in docker-compose)
CMD ["uvicorn", "api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
