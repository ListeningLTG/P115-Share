# Build Stage for Frontend
FROM node:20-alpine AS build-stage
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --network-timeout 300000
COPY frontend/ ./
RUN npm run build

# Production Stage
FROM python:3.12-slim
WORKDIR /app

# Create data directory for database and config
RUN mkdir -p /app/data

# Install dependencies
COPY backend/requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt


# Copy backend code
COPY backend/app ./app
COPY backend/migrations ./migrations
COPY backend/alembic.ini ./alembic.ini
COPY backend/entrypoint.sh ./entrypoint.sh

# Make entrypoint executable
RUN chmod +x ./entrypoint.sh

# Copy frontend build to static folder
COPY --from=build-stage /app/frontend/dist /app/static

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Expose port
EXPOSE 8000

# Start command
ENTRYPOINT ["./entrypoint.sh"]
