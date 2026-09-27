FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8080
EXPOSE 8080
# ONE worker on purpose: all state (contexts, conversations) lives in memory.
CMD ["sh", "-c", "uvicorn vera.app:app --host 0.0.0.0 --port ${PORT} --workers 1"]
