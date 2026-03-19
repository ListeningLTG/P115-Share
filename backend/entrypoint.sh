#!/bin/sh
set -e
echo "Starting application..."
echo "Running database migrations..."
python -m alembic upgrade head
echo "Starting server..."
exec python -m app.main
