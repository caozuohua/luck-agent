FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --shell /usr/sbin/nologin luck-agent

WORKDIR /opt/luck-agent

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /opt/luck-agent/data /opt/luck-agent/workspace \
    && chown -R luck-agent:luck-agent /opt/luck-agent

USER luck-agent

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health', timeout=3).read()" || exit 1

CMD ["python", "main.py"]
