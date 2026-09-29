# Docker

Per-service Dockerfiles live alongside their code (`backend/Dockerfile`, `frontend/Dockerfile`) and are
wired together by the root [`docker-compose.yml`](../docker-compose.yml). This directory is reserved for
shared Docker assets (e.g. common base images) if they become necessary in later phases.
