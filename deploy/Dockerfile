# Cloud Run Job image. Same build->smoke-test->deploy shape as the retail pipeline.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg espeak-ng ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN pip install --no-cache-dir -e .

# Cloud Run Job entrypoint: poll sources + run the pipeline once, then exit.
ENTRYPOINT ["python", "-m", "reelforge", "watch"]
