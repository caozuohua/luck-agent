FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --shell /usr/sbin/nologin agent

WORKDIR /home/agent/app

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py settings.py ./
COPY core/__init__.py core/agent.py core/intent_classifier.py core/log.py core/output_parser.py core/prompt_builder.py core/redaction.py core/result_summarizer.py core/router.py core/tool_executor.py core/
COPY interface/__init__.py interface/health.py interface/lark_ws.py interface/
COPY llm/__init__.py llm/vertex_client.py llm/
COPY memory/__init__.py memory/context_store.py memory/curator.py memory/db.py memory/goal_store.py memory/pattern_store.py memory/
COPY tools/__init__.py tools/base.py tools/registry.py tools/web_search.py tools/shell.py tools/
COPY config/routing_rules.yaml config/routing_rules.yaml
COPY soul/SOUL.md soul/SOUL.md

RUN mkdir -p /home/agent/data /home/agent/workspace \
    && chown -R agent:agent /home/agent

USER agent

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health', timeout=3).read()" || exit 1

CMD ["python", "main.py"]
