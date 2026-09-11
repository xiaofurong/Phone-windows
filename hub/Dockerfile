FROM python:3.11-slim
WORKDIR /app
COPY hub.py ./
COPY public ./public
ENV HUB_PORT=8080 \
    PC_AGENT_URL=http://192.168.31.59:8765 \
    HUB_TOKEN=qyx2026
EXPOSE 8080
CMD ["python", "hub.py"]
