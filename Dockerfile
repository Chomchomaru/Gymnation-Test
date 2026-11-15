FROM python:3.12-slim AS base
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir fastapi==0.163.1 pydantic==2.14.3 uvicorn==0.54.0
COPY main.py phone_validation.py billing.py reconciliation.py ./

# docker build --target test .
FROM base AS test
RUN pip install --no-cache-dir pytest==9.2.0 httpx==0.28.1
COPY tests ./tests
RUN python -m pytest -q tests

# docker build -t app . && docker run -p 8080:8080 app
FROM base AS app
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
