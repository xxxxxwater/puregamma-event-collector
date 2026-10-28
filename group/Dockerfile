FROM python:3.13-slim
RUN pip install --no-cache-dir telethon==1.45.0
WORKDIR /app
COPY src/group_feed.py src/group_reader.py /app/
CMD ["python", "/app/group_reader.py"]
