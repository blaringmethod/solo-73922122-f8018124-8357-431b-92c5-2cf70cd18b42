FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv/app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY wsgi.py ./

EXPOSE 8000

# 单进程多线程：SQLite(WAL) 下一个写者 + 多个读者；
# --threads 保证遍历大型档案的核查请求期间，补传请求仍可被处理。
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "300", "wsgi:app"]
