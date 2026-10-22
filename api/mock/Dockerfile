FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

EXPOSE 9000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "9000"]