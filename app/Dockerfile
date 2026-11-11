FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

EXPOSE 8000

# 默认以 gunicorn 启动 Web 进程（2 进程 × 4 线程，只读写作业队列，响应迅速）；
# 执行器进程由 docker-compose 以 command: python worker.py 覆盖启动。
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "60", "server:app"]
