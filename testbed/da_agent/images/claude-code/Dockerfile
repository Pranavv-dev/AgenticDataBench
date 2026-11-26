# Claude Code image for the Harbor parity experiment: the Harbor adapter's task
# image (harbor-index/adapters, src/agenticdatabench task-template/environment/
# Dockerfile) with Claude Code installed exactly as Harbor 0.27.0's claude-code
# agent installs it there. The agent runs as root, as on Harbor; upstream ran it
# as a non-root user named after the host user.
#
# Left out of the adapter's image: the domain data (upstream copies it into the
# mounted work dir instead), the plot helper (upstream copies it too) and the
# grader's venv in /opt/adb-verifier (upstream grades on the host).
FROM python:3.10-bullseye@sha256:65c6bdcca22db1a3468a0262ae05f38a0a718f51e9818cd09c12094aaae28a33

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Debian 11 (bullseye) packages are now only on archive.debian.org.
RUN sed -i 's|http://deb.debian.org|http://archive.debian.org|g' /etc/apt/sources.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev libssl-dev libffi-dev libxml2-dev libxslt1-dev zlib1g-dev curl \
    && rm -rf /var/lib/apt/lists/*

# Upstream's package list, pinned to the versions the adapter's image installs
# (current at upstream's last commit, 2026-10-13).
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt

WORKDIR /workspace

# harbor 0.27.0 ClaudeCode.install() in the adapter's image, recorded by
# harbor_parity/test_identical_launch.py and run here byte for byte, as root:
# 0. BaseInstalledAgent.setup().
RUN ["bash", "-c", "[ -d /installed-agent ] || mkdir -p /installed-agent"]
# 1. ensure_system_dependencies(curl, bash, nodejs, npm, procps): this image
#    lacks only nodejs and npm.
RUN ["bash", "-c", "(set -o pipefail) 2>/dev/null && set -o pipefail; apt-get update && apt-get install -y nodejs npm"]
# 2. The musl check (a no-op on Debian).
RUN ["bash", "-c", "(set -o pipefail) 2>/dev/null && set -o pipefail; if command -v apk >/dev/null 2>&1; then apk add --no-cache libgcc libstdc++; fi"]
# 3. Claude Code 2.1.311 with the native installer, as the agent user (root).
RUN ["bash", "-c", "(set -o pipefail) 2>/dev/null && set -o pipefail; set -euo pipefail; curl -fsSL https://downloads.claude.ai/claude-code-releases/bootstrap.sh | bash -s -- 2.1.311 && echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.bashrc && export PATH=\"$HOME/.local/bin:$PATH\" && claude --version"]

CMD ["/bin/bash"]
