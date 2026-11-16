FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV HOST=0.0.0.0
ENV PORT=8000

# Copy application files
COPY . /app

# Ensure persistent data directory exists
RUN mkdir -p /app/data

# Expose HTTP port
EXPOSE 8000

# Start server
CMD ["python", "-m", "app.server"]
