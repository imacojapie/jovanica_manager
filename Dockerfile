FROM python:3.12-slim
WORKDIR /app
COPY bot.py wiro_manager.py /app/
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
CMD ["python", "wiro_manager.py"]