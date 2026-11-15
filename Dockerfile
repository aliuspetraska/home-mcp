FROM python:3.13-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Cloud Run injects PORT (default 8080); the app reads it automatically.
ENV HOME_MCP_HTTP_HOST=0.0.0.0

CMD ["python", "-m", "app", "--http"]
