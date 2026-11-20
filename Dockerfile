# ZarailensAI web app container (optional; Vercel does not need it). Any Docker host: Cloud Run, Render, Railway…
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY . .
ENV PORT=8000
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://localhost:{os.environ.get(\"PORT\",\"8000\")}/api/health')"
# Long Earth Engine steps can take a few minutes: generous keep-alive / timeouts.
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT} --timeout-keep-alive 75
