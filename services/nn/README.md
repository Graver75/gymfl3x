# NN coach service (Ollama + FastAPI)

## Запуск

```bash
# только бот
docker compose up -d

# бот + ollama + nn
docker compose --profile nn up -d --build

# один раз скачать модель (~1GB для 1.5b)
docker compose --profile nn exec ollama ollama pull qwen2.5:1.5b-instruct
```

При лагах сервера освободить RAM:

```bash
docker compose --profile nn stop
```

Бот продолжит работать; в Telegram статус «офлайн».

API: `GET /health`, `POST /v1/coach` на порту 8000 (из сети compose: `http://nn:8000`).
