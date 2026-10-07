FROM docker.io/library/python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock pyproject.toml ./
COPY dial_in_trigger ./dial_in_trigger
RUN pip install --no-cache-dir -c requirements.lock . && \
    groupadd --gid 10001 trigger && \
    useradd --uid 10001 --gid 10001 --home-dir /data --no-create-home trigger
USER 10001:10001
EXPOSE 8787
CMD ["gunicorn", "--bind", "0.0.0.0:8787", "--workers", "2", "--threads", "2", "dial_in_trigger.web:create_app()"]

