# Архитектура агента сбора ссылок

## 1. Общее описание
Сервис запускается как контейнерное CLI-приложение (Typer). На вход передаются:
- путь к общей конфигурации (runtime/sheet/network/dedupe);
- каталог конфигов сайтов.

CLI инициализирует раннер (`app.workflow.runner.AgentRunner`), который поэтапно загружает конфигурации, подготавливает окружение (логи, state, Google Sheets) и распределяет работу между сайтами.

## 2. Компоненты
- `app.cli` — интерфейс командной строки, валидирует параметры запуска.
- `app.workflow.runner` — координация этапов выполнения (config -> state -> crawler -> sheets).
- `app.config` — модели и загрузчик конфигураций (общая YAML и отдельные конфиги сайтов, включая `selectors.content_drop_after` для среза контента товаров).
- `app.crawler` — движки обхода (HTTP и Playwright), пагинация и дедуп на уровне запуска.
- `app.sheets` — клиент Google Sheets (OAuth2, batchUpdate, вкладки `_state` и `_runs`, поддержка доменной импёрсонации сервисного аккаунта через `GOOGLE_OAUTH_IMPERSONATED_USER`, автосоздание строк заголовков вкладок (`source_site`, `category`, `category_url`, `product_url`, …, `llm_raw`).
- `app.state` — локальное хранилище (SQLite/JSONL) и синхронизация со скрытой вкладкой `_state`.
- `app.logger` — единая точка настройки Rich-логов.

## 2.1 Управление конфигурациями
- `app.config.loader.load_global_config` читает YAML/JSON с общими параметрами **или** строит объект `GlobalConfig` из переменных окружения (блоки `SHEET_*`, `RUNTIME_*`, `NETWORK_*`, `DEDUPE_*`, `STATE_*`).
- `app.config.loader.iter_site_configs` собирает все файлы сайта (`*.yml`, `*.yaml`, `*.json`) из каталога, валидирует их через Pydantic.
- Модели (`app.config.models`) описывают SheetConfig/Runtime/Network/Dedupe/State, а также SiteConfig с wait/stop conditions и лимитами.
- CLI принимает `--resume`, `--reset-state`, `--dry-run`, параметры путей можно передать через `.env` (`SITE_CONFIG_DIR`, `GLOBAL_CONFIG_PATH`).
- Переменная `PRODUCT_IMAGE_DIR` задаёт каталог, где складываются изображения товаров; путь передаётся в `RuntimeContext`.

## 2.2 Локальное состояние
- `app.state.storage.StateStore` создаёт SQLite-базу (`/var/app/state/runtime.db` по умолчанию) и таблицу `category_state`.
- Метод `upsert` сохраняет прогресс по `site_name + category_url` (last_page, last_offset, last_run_ts).
- Доступные операции: `get`, `iter_site_state`, `reset_site`, `reset_category`, `reset_all`.
- Эти данные синхронизируются с вкладкой `_state` (см. будущие этапы), что обеспечивает возобновляемость.

## 3. Потоки данных
1. CLI принимает параметры/окружение и формирует `RunnerOptions`.
2. Раннер читает `.env`, собирает глобальную конфигурацию (без обязательного файла) и загружает конфиги сайтов, агрегируя настройки (runtime/network/dedupe).
3. На основе конфигурации поднимается слой state (SQLite файл + кэш Google `_state`).
4. Для каждого сайта создаётся `SiteCrawler`, который:
   - выбирает движок (HTTP или Playwright) исходя из `engine`;
   - обрабатывает все `category_urls`, поддерживая пагинацию и фильтры;
   - пишет результаты в буфер `SiteCrawlResult`.
5. Буфер передаётся в `sheets.GoogleSheetsClient`, который пакетно коммитит строки и обновляет `_runs`.

## 4. Формат конфигураций
### Общая конфигурация (YAML / ENV)
```yaml
sheet:
  spreadsheet_id: "GOOGLE_SHEET_ID"
  write_batch_size: 200
  sheet_state_tab: "_state"
  sheet_runs_tab: "_runs"
runtime:
  max_concurrency_per_site: 2
  global_stop:
    stop_after_products: 50000
    stop_after_minutes: 180
network:
  user_agents: ["Mozilla/5.0 …", "Mozilla/5.0 …"]
  proxy_pool: []
  request_timeout_sec: 30
  retry:
    max_attempts: 3
    backoff_sec: [2, 5, 10]
dedupe:
  strip_params_blacklist: ["utm_*", "gclid", "yclid", "fbclid"]
state:
  driver: "sqlite"
  database: "/var/app/state/runtime.db"
```
Эквивалентные значения могут передаваться через `.env` переменные:
`SHEET_SPREADSHEET_ID`, `SHEET_WRITE_BATCH_SIZE`, `SHEET_STATE_TAB`, `SHEET_RUNS_TAB`,
`RUNTIME_MAX_CONCURRENCY_PER_SITE`, `RUNTIME_STOP_AFTER_PRODUCTS`, `NETWORK_USER_AGENTS`,
`NETWORK_PROXY_POOL`, `NETWORK_REQUEST_TIMEOUT_SEC`, `NETWORK_RETRY_MAX_ATTEMPTS`,
`NETWORK_RETRY_BACKOFF_SEC`, `DEDUPE_STRIP_PARAMS_BLACKLIST`, `STATE_DRIVER`, `STATE_DATABASE_PATH`.

### Конфигурация сайта
```yaml
site:
  name: "alcoplaza"
  domain: "alcoplaza.ru"
  base_url: "https://alcoplaza.ru"
  engine: "http"   # http | browser
  wait_conditions:
    - type: "selector"
      value: ".product-card"
  stop_conditions:
    - type: "missing_selector"
      value: ".pagination"
selectors:
  product_link_selector: ".product-card a.product-link"
  next_button_selector: "a.next"
pagination:
  mode: "numbered_pages"
  param_name: "page"
  max_pages: 50
limits:
  max_products: 2000
  max_scrolls: 30
category_urls:
  - "https://alcoplaza.ru/catalog/vodka/"
```

## 5. Контейнеризация
- Docker образ на базе `python:3.12-slim`, установка зависимостей из `requirements.txt`.
- Запуск только через `python -m app.main`.
- Конфиги монтируются в `/app/config`, состояние и credentials — в `/var/app/state` и `/secrets`.

## 6. Файл `.env`
Используется для передачи путей к конфигурациям, state и OAuth-файлам. Все переменные снабжены комментариями с описанием источников доступа. Переменная `WRITE_FLUSH_PRODUCT_INTERVAL` определяет, как часто (в товарах) агент будет отправлять накопленные данные в Google Sheets, чтобы итоговые записи появлялись даже при прерывании запуска (для обратной совместимости поддерживается `WRITE_FLUSH_PAGE_INTERVAL`).

## 7. Следующие этапы
По завершении каждого этапа (config/state/crawler/sheets/надёжность) этот документ будет дополняться деталями реализации и диаграммами потоков.

## 8. Этап 3 — модуль обхода
- `app.crawler.engines` реализует `HttpEngine` (httpx + ретраи) и `BrowserEngine` (Playwright sync API, скролл для infinite_scroll). Общий интерфейс `EngineRequest`.
- `app.crawler.site_crawler.SiteCrawler` поддерживает все три режима пагинации, wait/stop-conditions, счётчики, дедуп, обновление `StateStore`, а также умеет отдавать данные порциями каждые `WRITE_FLUSH_PRODUCT_INTERVAL` товаров.
- `app.crawler.service.CrawlService` поочерёдно запускает `SiteCrawler` для каждого сайта и возвращает список `SiteCrawlResult`.
- `app.crawler.content_fetcher.ProductContentFetcher` скачивает карточку товара, извлекает текст без тегов и отдаёт ссылку на основное изображение, а сохранением файлов занимается `app.media.image_saver.ImageSaver` в момент записи строки (что гарантирует появление только "валидных" изображений).
- Нормализация ссылок и md5-хэш находятся в `app.crawler.utils.normalize_url`.

## 9. Этап 4 — запись в Google Sheets
- `app.sheets.client.GoogleSheetsClient` инкапсулирует OAuth2 (InstalledAppFlow), проверку/создание вкладок, batchUpdate и повторные попытки с экспоненциальным бэкоффом.
- `app.sheets.writer.SheetsWriter` превращает `ProductRecord` в строки (колонки A–L), добавляя очищенный контент и путь к изображению рядом с URL товара, и умеет дозаписывать данные порциями по мере обхода (кэшируя уже существующие URL, чтобы избежать дубликатов).
- Вкладки `_runs` и `_state` создаются автоматически; `_runs` получает итоги (run_id, site, started/finished, totals), `_state` отражает содержимое SQLite-хранилища для возобновляемости.
- `AgentRunner` вызывает SheetsWriter после краулера (если не указан dry-run), сохраняя список последних результатов внутри раннера.

## 10. Этап 5 — надёжность и тесты
- Антиблок: ротация User-Agent/прокси, HTTP-ретраи с экспоненциальными задержками, случайный джиттер между загрузками страниц.
- Логирование через `rich` + структурированные сообщения для ключевых событий (старт/обход/запись, предупреждения по состоянию).
- Покрытие тестами (`pytest`): загрузчик конфигов, дедуп (normalize_url), state store, site crawler (пагинация и резюмируемость), SheetsWriter (моки клиента).
- Dockerfile устанавливает Playwright + системные зависимости, что гарантирует воспроизводимость внутри контейнера.
