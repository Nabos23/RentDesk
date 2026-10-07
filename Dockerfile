FROM python:3.12-slim

# ffmpeg needed if we want to convert audio formats beyond what pydub handles natively
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cached layer)
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

# Copy application code
COPY app/ ./app/

# Copy env template (user must provide real .env)
COPY .env.example .env.example

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
