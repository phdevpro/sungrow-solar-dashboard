FROM python:3.12-slim

WORKDIR /agent
RUN pip install --no-cache-dir "httpx>=0.27"
COPY main.py .

CMD ["python", "main.py"]
