FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn[standard] pydantic httpx

COPY runner.py /app/tool_runtime/runner.py
COPY server.py /app/tool_runtime/server.py

RUN mkdir -p /app/custom_tools/skill_data

EXPOSE 8090

CMD ["uvicorn", "tool_runtime.server:app", "--host", "0.0.0.0", "--port", "8090"]
