FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY zec_bot.py .
ENV PYTHONUNBUFFERED=1 STATE_FILE=/data/alert_state.json
CMD ["python", "zec_bot.py"]
