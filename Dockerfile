FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY musicme_client.py server.py ./

EXPOSE 4533

ENTRYPOINT ["python3", "server.py"]
