# A Plow cloud agent image. The contract it satisfies is in
# https://github.com/plow-pbc/plow/blob/main/api/cloud-agents/README.md
FROM python:3.13-slim

RUN groupadd --gid 10000 agent \
 && useradd --uid 10000 --gid 10000 --create-home agent

RUN pip install --no-cache-dir websockets==17.1

COPY --chown=10000:10000 agent.py /opt/agent/agent.py

# Everything the agent needs arrives in its environment, so nothing has to
# start as root.
USER 10000:10000
WORKDIR /home/agent
ENV HOME=/home/agent

# No EXPOSE: these VMs are never published and nothing dials in. The chat
# surface is an outbound WebSocket.
#
# The CMD is PID 1. Nothing supervises it from outside the image, and exec form
# is what makes SIGTERM reach the process rather than a shell.
CMD ["python3", "-u", "/opt/agent/agent.py"]
