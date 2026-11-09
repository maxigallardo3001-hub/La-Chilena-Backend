FROM python:3.12-slim
ARG SERVICE
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common/lachilena_common /app/lachilena_common
COPY services/${SERVICE}/app /app/app
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
RUN useradd --system --uid 10001 appuser
USER appuser
CMD ["python", "-m", "lachilena_common.run", "app.main:app"]
