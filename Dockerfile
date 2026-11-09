FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[api]"

RUN useradd --create-home tally
USER tally

EXPOSE 8000
CMD ["uvicorn", "tally.api:app", "--host", "0.0.0.0", "--port", "8000"]
