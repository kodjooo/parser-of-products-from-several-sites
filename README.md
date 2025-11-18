# Агент сбора ссылок товаров

Контейнеризованный CLI-сервис обходит категории интернет-магазинов (HTTP или Playwright), нормализует ссылки на товары и пакетно записывает их в Google Sheets (каждый домен — отдельная вкладка). Агент поддерживает возобновление с последнего состояния, дедупликацию, учёт лимитов и журналирование итогов запуска.

## Основные компоненты
- `app/cli.py` — Typer-CLI (`python -m app.main ...`).
- `app/config` — pydantic-модели и загрузчик YAML/JSON конфигов.
- `app/crawler` — движки обхода (httpx и Playwright), пагинация и дедуп.
- `app/state` — локальное SQLite-хранилище прогресса, синхронизируется с вкладкой `_state`.
- `app/sheets` — OAuth2 + Google Sheets API (batchUpdate, вкладки `_runs`/`_state`).
- `tests/` — pytest с моками (config/state/crawler/sheets).

## Подготовка окружения
1. Отредактируйте `.env` (см. `.env.example`):
   - блок Google (пути к JSON, токену и scopes); поддерживаются как desktop OAuth (с сохранением токена), так и service account JSON (тип `service_account`, токен не требуется);
   - для сервисного аккаунта с делегированием доменных прав задайте `GOOGLE_OAUTH_IMPERSONATED_USER` — email пользователя Google Workspace, к которому есть доступ к таблице;
   - блок `SHEET_*`, `RUNTIME_*`, `NETWORK_*`, `DEDUPE_*`, `STATE_*` — все рабочие параметры теперь задаются через `.env`;
   - `SITE_CONFIG_DIR` — путь внутри контейнера, куда будет примонтирован каталог с конфигами сайтов;
   - `WRITE_FLUSH_PRODUCT_INTERVAL` — через сколько товаров отправлять накопленный буфер в Google Sheets (по умолчанию 5, чтобы записи появлялись даже при прерывании запуска).
   - `PRODUCT_IMAGE_DIR` — каталог внутри контейнера, где будут храниться скачанные изображения товаров (смонтируйте volume).
2. Сформируйте конфиги сайтов `config/sites/*.yml` (selectors, pagination, limits, wait/stop conditions, список `category_urls`) и примонтируйте каталог в `SITE_CONFIG_DIR`.
   - В блоке selectors можно указать `content_drop_after` — список CSS-селекторов, после которых (включая соответствующие элементы) текст товара не попадёт в `product_content`. Это полезно для удаления блоков отзывов/рекомендаций.
   - Для дополнительных полей предусмотрите селекторы: `name_en_selector`, `name_ru_selector`, `price_without_discount_selector`, `price_with_discount_selector`, а также словарь `category_labels` (ключ — slug из URL после `/items/`, значение — человекочитаемое название категории в таблице). Для `price_with_discount_selector` можно передать список селекторов — агент пойдёт по нему сверху вниз, пока не найдёт цену.
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
  python -m app.main
```

### Параметры CLI
- `--run-id` — задаём свой UUID (иначе генерируется).
- `--resume/--no-resume` — продолжать с учётом локального state.
- `--reset-state` — очистить SQLite перед запуском.
- `--dry-run` — выполнить обход без записи в Google Sheets.

## OAuth и Google Sheets
1. Создайте OAuth Client (Desktop) в Google Cloud, скачайте JSON → путь в `.env`.
2. Первый запуск запросит код авторизации в консоли; токен сохранится в `GOOGLE_OAUTH_TOKEN_PATH`.
3. Агент сам создаёт вкладки `<домен>`, `_state`, `_runs` и проставляет заголовок первой строки. Для вкладки сайта используются столбцы:
   - A `source_site`
   - B `category` (часть URL после `/items/`)
   - C `category_url`
   - D `product_url`
   - E `product_content` (очищенный текст страницы товара без тегов/стилей)
   - F `discovered_at`
   - G `run_id`
   - H `product_id_hash`
   - I `page_num`
   - J `metadata` (в том числе `image_url`)
   - K `image_path` (только имя файла, лежащего в `PRODUCT_IMAGE_DIR`)
   - L `name (en)`
   - M `name (ru)`
   - N `price (without discount)`
   - O `price (with discount)`
   - P `status`
   - Q `note`
   - R `processed_at`
   - S `llm_raw`

## Возобновляемость и state
- Локальный SQLite (`state.runtime.db`) хранит `last_page`, `last_product_count`, `last_run_ts` на каждую категорию.
- После обхода содержимое экспортируется в скрытую вкладку `_state`, что позволяет отследить, где остановился агент.
- Для надёжности данные пишутся порциями: каждые `WRITE_FLUSH_PRODUCT_INTERVAL` товаров (по умолчанию 5) агент сразу отправляет накопленные записи в Google Sheets.

## Контент и изображения товаров
- После нахождения ссылки агент переходит по URL, выгружает страницу целиком, очищает её от тегов/скриптов и записывает текст в колонку `product_content`.
- Главное изображение определяется по `og:image`, `srcset` или первому `<img>` и сохраняется только для тех строк, которые реально попадут в Google Sheets (после успешной записи). Файлы кладутся в `PRODUCT_IMAGE_DIR`, имя формируется транслитом названия товара.
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
  python -m app.main
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
