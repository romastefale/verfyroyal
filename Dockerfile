FROM python:3.12-slim
WORKDIR /app
RUN mkdir -p /data
COPY main.py ./main.py
CMD ["python", "main.py"]
