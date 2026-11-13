# demo-app container image.
#
# Deployment contract (taskflow-docs/deployment.md): migrations run
# before the new container starts, so the entrypoint migrates first,
# then serves. The SQLite database lives on the /data volume.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py digest.py ./
COPY scripts/ scripts/

ENV TASKFLOW_DB_PATH=/data/taskflow.db
VOLUME ["/data"]
EXPOSE 8000

CMD ["sh", "-c", "python scripts/migrate.py && uvicorn main:app --host 0.0.0.0 --port 8000"]
