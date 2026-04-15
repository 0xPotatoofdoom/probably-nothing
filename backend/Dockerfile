FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000

# Single image, two modes:
#   - `docker run probably-nothing <github-url>`           → standalone CLI audit
#   - `docker run probably-nothing serve`                  → FastAPI + WebSocket server
#   - `docker run probably-nothing python -m cli --help`   → raw CLI
# docker-compose invokes the server mode via the `serve` command.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["--help"]
