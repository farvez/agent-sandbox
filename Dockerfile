FROM python:3.11-slim

# Install strace for system call analysis (Step 3)
RUN apt-get update && apt-get install -y --no-install-recommends \
    strace \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Run as non-root user for security
RUN useradd -m -u 1000 sandboxuser
WORKDIR /workspace
RUN chown sandboxuser:sandboxuser /workspace

USER sandboxuser
CMD ["/bin/bash"]