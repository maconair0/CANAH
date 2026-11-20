# The CANAH sandbox image: where the agent's `execute` runs.
# Built automatically on first start (canah/sandbox/docker.py); by hand:
#   docker build -t canah-sandbox:1 canah/sandbox
# The container idles (`sleep infinity`); commands go in with `docker exec`, so it needs no network.
FROM python:3.12-slim

# Tools the agent's shell commands and deepagents' file helpers use.
RUN apt-get update \
 && apt-get install -y --no-install-recommends coreutils findutils grep sed gawk procps jq ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir numpy==2.3.5 \
 && useradd --uid 1000 --create-home --shell /bin/bash agent \
 && mkdir -p /workspace && chown agent:agent /workspace

USER agent
WORKDIR /workspace
ENV HOME=/workspace USER=agent LOGNAME=agent SHELL=/bin/bash PYTHONDONTWRITEBYTECODE=1
CMD ["sleep", "infinity"]
