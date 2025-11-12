# Агент сбора ссылок товаров

Контейнеризованный CLI-сервис обходит категории интернет-магазинов (HTTP или Playwright), нормализует ссылки на товары и пакетно записывает их в Google Sheets (каждый домен — отдельная вкладка). Агент поддерживает возобновление с последнего состояния, дедупликацию, учёт лимитов и журналирование итогов запуска.

## Основные компоненты
- `app/cli.py` — Typer-CLI (`python -m app.main run ...`).
- `app/config` — pydantic-модели и загрузчик YAML/JSON конфигов.
- `app/crawler` — движки обхода (httpx и Playwright), пагинация и дедуп.
- `app/state` — локальное SQLite-хранилище прогресса, синхронизируется с вкладкой `_state`.
- `app/sheets` — OAuth2 + Google Sheets API (batchUpdate, вкладки `_runs`/`_state`).
- `tests/` — pytest с моками (config/state/crawler/sheets).

## Подготовка окружения
1. Отредактируйте `.env` (см. `.env.example`):
   - блок Google (пути к JSON, токену и scopes); поддерживаются как desktop OAuth (с сохранением токена), так и service account JSON (тип `service_account`, токен не требуется);
   - блок `SHEET_*`, `RUNTIME_*`, `NETWORK_*`, `DEDUPE_*`, `STATE_*` — все рабочие параметры теперь задаются через `.env`;
   - `SITE_CONFIG_DIR` — путь внутри контейнера, куда будет примонтирован каталог с конфигами сайтов;
   - `WRITE_FLUSH_PAGE_INTERVAL` — через сколько страниц отправлять накопленные записи в Google Sheets (по умолчанию 5, чтобы данные появлялись даже при прерывании запуска).
   - `PRODUCT_IMAGE_DIR` — каталог внутри контейнера, где будут храниться скачанные изображения товаров (смонтируйте volume).
2. Сформируйте конфиги сайтов `config/sites/*.yml` (selectors, pagination, limits, wait/stop conditions, список `category_urls`) и примонтируйте каталог в `SITE_CONFIG_DIR`.
3. Если планируете браузерный движок локально, выполните `playwright install chromium`.

## Сборка и запуск в Docker
```bash
# Сборка образа
docker build -t products-agent .

# Запуск (монтируем конфиги сайтов, state и секреты; глобальные параметры берутся из .env)
docker run --rm \
  --env-file .env \
  -v $(pwd)/config/sites:/app/config/sites \
  -v $(pwd)/state:/var/app/state \
  -v $(pwd)/assets/images:/app/assets/images \
  -v $(pwd)/secrets:/secrets \
  products-agent \
  python -m app.main run
```

### Параметры CLI
- `--run-id` — задаём свой UUID (иначе генерируется).
- `--resume/--no-resume` — продолжать с учётом локального state.
- `--reset-state` — очистить SQLite перед запуском.
- `--dry-run` — выполнить обход без записи в Google Sheets.

## OAuth и Google Sheets
1. Создайте OAuth Client (Desktop) в Google Cloud, скачайте JSON → путь в `.env`.
2. Первый запуск запросит код авторизации в консоли; токен сохранится в `GOOGLE_OAUTH_TOKEN_PATH`.
3. Агент сам создаёт вкладки `<домен>`, `_state`, `_runs`. Для вкладки сайта используются столбцы:
   - A `source_site`
   - B `category_url`
   - C `product_url`
   - D `product_content` (очищенный текст страницы товара без тегов/стилей)
   - E `discovered_at`
   - F `run_id`
   - G `status`
   - H `note`
   - I `product_id_hash`
   - J `page_num`
   - K `metadata` (в том числе `image_url`)
   - L `image_path` (локальный путь к сохранённому файлу из `PRODUCT_IMAGE_DIR`)

## Возобновляемость и state
- Локальный SQLite (`state.runtime.db`) хранит `last_page`, `last_product_count`, `last_run_ts` на каждую категорию.
- После обхода содержимое экспортируется в скрытую вкладку `_state`, что позволяет отследить, где остановился агент.
- Для надёжности данные пишутся порциями: каждые `WRITE_FLUSH_PAGE_INTERVAL` страниц (по умолчанию 5) агент сразу отправляет накопленные записи в Google Sheets.

## Контент и изображения товаров
- После нахождения ссылки агент переходит по URL, выгружает страницу целиком, очищает её от тегов/скриптов и записывает текст в колонку `product_content`.
- Главное изображение определяется по `og:image`, `srcset` или первому `<img>`, скачивается в `PRODUCT_IMAGE_DIR`, имя формируется транслитом названия товара.
- Путь к локальному файлу попадает в колонку `image_path`, а исходный URL фиксируется в `metadata` (ключ `image_url`).

## Тесты
```bash
docker build -t products-agent .
docker run --rm \
  -v $(pwd):/app \
  products-agent \
  python -m pytest
```
Тесты покрывают загрузчик конфигов, state store, утилиты дедупликации, site crawler (пагинация/резюмируемость) и SheetsWriter (моки API).

## Деплой на удалённый сервер
1. Скопируйте исходники и секреты (`scp -r . user@host:/opt/agent`).
2. На сервере установите Docker и выполните:
   ```bash
   cd /opt/agent
   docker build -t products-agent .
   ```
3. Создайте директории `/opt/agent/config/sites`, `/opt/agent/state`, `/opt/agent/assets/images`, `/opt/agent/secrets` и положите туда конфиги/volume (копируйте из репозитория/CI).
4. Запускайте контейнер командой:
   ```bash
   docker run --rm \
     --env-file /opt/agent/.env \
     -v /opt/agent/config/sites:/app/config/sites \
     -v /opt/agent/state:/var/app/state \
     -v /opt/agent/assets/images:/app/assets/images \
     -v /opt/agent/secrets:/secrets \
     products-agent \
     python -m app.main run
   ```
5. Для регулярных запусков создайте systemd unit или cron-задачу, использующую эту команду (опционально добавьте `--dry-run`, `--no-resume` при необходимости).

## Docker Compose
Для упрощения запуска используйте `docker-compose.yml`, который уже описывает сервис `parser`:

```bash
# старт с пересборкой
docker compose up --build parser

# или только запуск (если образ уже собран)
docker compose up parser
```

Команда автоматически подхватит `.env` из корня и смонтирует нужные volume (`config/sites`, `state`, `assets/images`, `secrets`). Каталоги должны существовать заранее.
