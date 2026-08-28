# syntax=docker/dockerfile:1
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json ./
RUN npm install
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir numpy matplotlib pyyaml "fastapi>=0.115" "uvicorn[standard]>=0.30"
COPY pyproject.toml README.md ./
COPY kline_match ./kline_match
COPY config ./config
COPY --from=web /web/dist ./web/dist
RUN pip install --no-cache-dir -e .
RUN mkdir -p data
EXPOSE 18765
CMD ["python", "-m", "kline_match", "serve", "--host", "0.0.0.0", "--port", "18765"]
