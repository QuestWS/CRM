FROM python:3.12-slim

# ffmpeg is needed to split stereo call recordings per speaker; without it
# transcription still works, just with weaker speaker attribution.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN pip install --no-cache-dir -e .

# Written to by the app: the database, recordings, Google token.
VOLUME ["/data"]
EXPOSE 8000

CMD ["python", "-m", "crm.cli", "serve", "--host", "0.0.0.0"]
