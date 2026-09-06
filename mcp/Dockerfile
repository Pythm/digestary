FROM python:3.12-slim
WORKDIR /mcp
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . /mcp
EXPOSE 8000
# FastMCP servers speak MCP over stdio by default; no exposed port needed.
CMD ["python", "mcp_server.py"]
