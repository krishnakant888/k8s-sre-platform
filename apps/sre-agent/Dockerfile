FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

# the release shown in every log line, set by CI to the short commit sha
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY agent ./agent

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001

# the workflow steps and the webhook deployment both set their own command
CMD ["python", "-m", "agent.steps"]
