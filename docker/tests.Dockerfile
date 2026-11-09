FROM python:3.12-slim
WORKDIR /tests
RUN pip install --no-cache-dir httpx==0.28.1 pytest==8.3.4
COPY tests /tests
