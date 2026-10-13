# The Ollama restart sidecar (#643). Stdlib-only Python, no pip installs, no repo code but its own
# script. It is the only container that mounts /var/run/docker.sock; see restarter.py's docstring
# for the one thing it does and the compose snippet in docker-compose.yml.example for how it runs
# (internal network, no published port, read-only root, all capabilities dropped).
FROM python:3.14-slim

COPY restarter.py /restarter.py

# PYTHONDONTWRITEBYTECODE: the root FS is read-only in compose anyway.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

EXPOSE 8080
CMD ["python3", "/restarter.py"]
