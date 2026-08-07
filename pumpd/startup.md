# pumpd startup

Startup and operations documentation lives at the repository root:

- **[../startup.md](../startup.md)** — first-time setup and starting the system
- **[../operations.md](../operations.md)** — day-to-day operation

Quick start:

```bash
cd pumpd
cp config.example.yaml config.yaml
cp .env.example .env
# edit config.yaml and .env
docker compose up -d --build
```

Dashboard: http://localhost:8080
