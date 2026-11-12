FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libsndfile1 libgomp1 && rm -rf /var/lib/apt/lists/*
COPY requirements*.txt ./
ARG DIARIZATION=0
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.5.1 torchaudio==2.5.1 && \
    if [ "$DIARIZATION" = "1" ]; then pip install --no-cache-dir -r requirements-diarization.txt; else pip install --no-cache-dir -r requirements.txt; fi
COPY *.py sop_rules.yaml ./
ENV DEVICE=cpu CALLGUARD_DATA_DIR=/data
EXPOSE 8000
CMD ["python", "main.py"]
