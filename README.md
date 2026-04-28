# Wikidata Cars

Собирает все модели автомобилей из [Wikidata](https://www.wikidata.org) (Q3231690 + подклассы)
с характеристиками (производитель, годы выпуска, тип кузова, масса, мощность, габариты,
объём двигателя, тип топлива, привод, количество дверей), складывает в SQLite
и поднимает веб-интерфейс для поиска / сравнения.

- Обновление данных: systemd-таймер, каждое воскресенье 03:00 UTC.
- Веб: FastAPI + Jinja (+ FTS5 для быстрого поиска).
- API: `/api/search?q=`, `/api/model/{qid}`, `/api/compare?qids=Q1,Q2`, `/api/stats`.
- Экспорт: `/export.csv` (UTF-8 BOM, дружит с Excel).

## Локальный запуск

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. (опционально) быстро заполнить БД — первые 500 ID, ~минута:
python scraper.py --limit 500

# Полный скрап (десятки тысяч моделей, ~20–60 мин):
python scraper.py

# 2. Поднять веб:
uvicorn app.main:app --reload --port 8510
# → http://127.0.0.1:8510/
```

БД по умолчанию: `./data/cars.db` (путь настраивается через `--db` или env `CARS_DB`).

## Деплой на VPS

```bash
curl -fsSL https://raw.githubusercontent.com/Mirzoevmurad/wikidata-cars/main/scripts/deploy_vps.sh -o deploy.sh
sudo DOMAIN=cars.play2go.cloud bash deploy.sh     # HTTPS через Caddy
# или без домена:
sudo bash deploy.sh                               # HTTP на :8510
```

Скрипт ставит python deps, регистрирует systemd-юниты
(`wikidata-cars.service`, `wikidata-cars-scrape.service` + `.timer`),
опционально настраивает Caddy с HTTPS и запускает первичный скрап в фоне.

Полезные команды на сервере:

```bash
systemctl status wikidata-cars
journalctl -u wikidata-cars -f
systemctl list-timers | grep wikidata-cars
systemctl start wikidata-cars-scrape    # ручной запуск скрапа
journalctl -u wikidata-cars-scrape -f
```

## Устройство

```
scraper.py                 # SPARQL → SQLite (cars + FTS5 + meta)
app/main.py                # FastAPI приложение
app/templates/*.html       # Jinja-шаблоны
app/static/app.css         # стили
scripts/deploy_vps.sh      # идемпотентный деплой
data/cars.db               # БД (ignored in git)
```

SQLite-схема: таблица `cars` (PRIMARY KEY `qid`, поля-характеристики + `updated_at`)
+ виртуальная таблица `cars_fts` (FTS5 по `label`, `manufacturer`) + `meta(key,value)`
для последнего времени обновления.

Скрапер идемпотентен (UPSERT по `qid`), не удаляет устаревшие записи — повторный
запуск только обновит поля существующих моделей и добавит новые.
