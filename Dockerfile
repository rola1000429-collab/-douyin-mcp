FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml server.py ./
RUN pip install --no-cache-dir .
ENV MCP_TRANSPORT=http
CMD ["python", "server.py"]
