FROM python:3.11-slim

# Install strace for system call analysis (Step 3)
RUN apt-get update && apt-get install -y --no-install-recommends \
    strace \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Run as non-root user for security. Its uid/gid match the host account that owns the
# workspaces (the server passes its "sandbox" user's ids), so files show as owned by
# "sandbox" inside the container instead of a bare number.
ARG SANDBOX_UID=1000
ARG SANDBOX_GID=1000
RUN groupadd -o -g "$SANDBOX_GID" sandbox \
    && useradd -o -m -u "$SANDBOX_UID" -g "$SANDBOX_GID" -s /bin/bash sandbox
WORKDIR /workspace
RUN chown sandbox:sandbox /workspace

USER sandbox
CMD ["/bin/bash"]
