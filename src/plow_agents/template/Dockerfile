# A Plow cloud agent image. The contract it satisfies is in
# https://github.com/plow-pbc/plow/blob/main/api/cloud-agents/README.md
FROM python:3.13-slim

# uid/gid 10000 is the contract, and `agent.py` drops to it itself. There is
# no USER here on purpose: Plow writes /var/lib/plow/credentials root-owned
# 0600, so PID 1 has to start as root to read it. An image that declares
# `USER 10000` cannot, and comes up with nothing to authenticate as.
RUN groupadd --gid 10000 agent \
 && useradd --uid 10000 --gid 10000 --create-home agent

RUN pip install --no-cache-dir websockets==17.1

COPY --chown=10000:10000 agent.py /opt/agent/agent.py

WORKDIR /home/agent
ENV HOME=/home/agent

# No EXPOSE: these VMs are never published and nothing dials in. The chat
# surface is an outbound WebSocket.
#
# The CMD is PID 1. Nothing supervises it from outside the image, and exec form
# is what makes SIGTERM reach the process rather than a shell.
CMD ["python3", "-u", "/opt/agent/agent.py"]
