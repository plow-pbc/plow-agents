# The feedback kitchen: a kitchen sous (plus its relay cooks) in tmux, on
# Swarm, filing issues with gh. It builds nothing, so it holds no checkouts.
# Not published: swarm is a private binary, so build it where you run it.
FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      tmux git curl jq ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code@2.1.319 && npm cache clean --force
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
RUN arch=$(dpkg --print-architecture) \
    && curl -fsSL https://github.com/cli/cli/releases/download/v2.92.0/gh_2.92.0_linux_$arch.tar.gz \
       | tar -xz --strip-components=2 -C /usr/local/bin gh_2.92.0_linux_$arch/bin/gh
ENV UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_INSTALL_DIR=/opt/uv-python
RUN uv tool install --python 3.12 git+https://github.com/srosro/kitchen@3edbd604785c5d5035a52c47c88e3d6aaf0ff0ad

COPY swarm /usr/local/bin/swarm
COPY CLAUDE.md /opt/feedback/CLAUDE.md
COPY bin/boot /opt/feedback/boot

RUN userdel -r node && useradd -m -u 1000 -s /bin/bash agent
USER agent
WORKDIR /home/agent
CMD ["/opt/feedback/boot"]
