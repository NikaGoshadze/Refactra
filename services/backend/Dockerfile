FROM node:24.22.3-bookworm-slim AS typescript-tools
WORKDIR /tools
COPY package.json package-lock.json ./
COPY apps/web/package.json ./apps/web/package.json
RUN npm ci --ignore-scripts --no-audit --no-fund

FROM python:3.12.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /usr/local/bin/uv
COPY --from=typescript-tools /usr/local/bin/node /usr/local/bin/node
COPY --from=typescript-tools /tools/node_modules/typescript /node_modules/typescript
COPY --from=typescript-tools /tools/node_modules/@types/react /node_modules/@types/react
COPY --from=typescript-tools /tools/node_modules/@types/react-dom /node_modules/@types/react-dom
COPY --from=typescript-tools /tools/node_modules/csstype /node_modules/csstype
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app
COPY services/backend/pyproject.toml services/backend/uv.lock ./
RUN uv sync --frozen --no-install-project
COPY services/backend/ ./
RUN uv sync --frozen && useradd --no-log-init --create-home --uid 10001 refactra && chown refactra:refactra /app
USER refactra
CMD ["uv", "run", "--no-sync", "uvicorn", "refactra.api:app", "--host", "0.0.0.0", "--port", "8000"]
