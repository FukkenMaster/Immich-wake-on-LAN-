FROM python:3.12-alpine
WORKDIR /app
RUN pip install --no-cache-dir flask==3.0.3
COPY app.py index.html ./
ENV PORT=18080
CMD ["python", "app.py"]
