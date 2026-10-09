FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "mcp>=1.8,<2" "playwright>=1.44" "uvicorn>=0.30" "browserbase>=1.0"
COPY server.py ./
ENV MCP_TRANSPORT=http
CMD ["python", "server.py"]
