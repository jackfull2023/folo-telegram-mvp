FROM python:3.12-slim

WORKDIR /app
COPY app.py config.example.json requirements.txt ./
RUN mkdir -p /app/data

ENV RADAR_CONFIG=/app/config.example.json
ENV RADAR_DB=/app/data/radar.sqlite
EXPOSE 8080

CMD ["python", "app.py", "serve"]
