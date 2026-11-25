FROM python:3.12-slim
WORKDIR /app
COPY app.py ./
COPY agent/ ./agent/
EXPOSE 8080
CMD ["python", "app.py"]
