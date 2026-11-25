# Pinned to the digest the Harbor adapter's task image uses.
FROM python:3.10-bullseye@sha256:65c6bdcca22db1a3468a0262ae05f38a0a718f51e9818cd09c12094aaae28a33

# Debian 11 (bullseye) packages are now only on archive.debian.org.
RUN sed -i 's|http://deb.debian.org|http://archive.debian.org|g' /etc/apt/sources.list

WORKDIR /workspace

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    libssl-dev \
    libffi-dev \
    libxml2-dev \
    libxslt1-dev \
    zlib1g-dev \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20.x
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip and install Python packages
RUN pip install --upgrade pip
# The same package list, pinned to the versions the Harbor adapter's task image
# installs (current at upstream's last commit, 2026-10-13).
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt

# Create a non-root user named after the HOST user, with a uid matching the
# HOST uid (both passed as build args) so bind-mounted /workspace files are
# owned by the host user. Falls back to uid 1000 / name "agent".
ARG HOST_USER=agent
ARG HOST_UID=1000
RUN apt-get update && apt-get install -y sudo && \
    useradd -m -u ${HOST_UID} -s /bin/bash ${HOST_USER} && \
    echo "${HOST_USER} ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers && \
    mkdir -p /home/${HOST_USER}/.claude && \
    chown -R ${HOST_USER}:${HOST_USER} /workspace /home/${HOST_USER} && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

USER ${HOST_USER}

# Claude Code 2.1.311, installed the way Harbor's claude-code agent installs it
# (native build into the agent user's ~/.local/bin), so both sides of parity run
# the same CLI.
RUN curl -fsSL https://downloads.claude.ai/claude-code-releases/bootstrap.sh | bash -s -- 2.1.311 \
    && echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
ENV PATH=/home/${HOST_USER}/.local/bin:${PATH}

CMD ["/bin/bash"]