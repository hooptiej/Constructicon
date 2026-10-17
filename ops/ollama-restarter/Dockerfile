# The Ollama restart sidecar (#643). Stdlib-only Python, no pip installs, no repo code but its own
# script. It is the only container that mounts /var/run/docker.sock; see restarter.py's docstring
# for the one thing it does and the compose snippet in docker-compose.yml.example for how it runs
# (internal network, no published port, read-only root, all capabilities dropped).
# Base image pinned by digest (#696), the SAME python:3.14-slim the app's Dockerfile pins (#676), so
# the box keeps one base layer and a rebuild can't pick up a different Python or Debian under the one
# container that holds the Docker socket. Bump it together with the root Dockerfile:
# `docker pull python:3.14-slim`, then
# `docker image inspect python:3.14-slim --format '{{index .RepoDigests 0}}'`, paste the new digest
# here AND in the root Dockerfile, rebuild both, and run scripts/check_image_layout.py (it fails on
# an unpinned FROM or when the two differ).
FROM python:3.14-slim@sha256:656d12e70054d5fda18a045e2494c96701e9792dd1445f95b3d038df954f57e9

COPY restarter.py /restarter.py

# PYTHONDONTWRITEBYTECODE: the root FS is read-only in compose anyway.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

EXPOSE 8080
CMD ["python3", "/restarter.py"]
