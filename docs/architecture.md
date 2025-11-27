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
- `app.config.runtime_paths` — вспомогательные функции, которые подставляют пути по умолчанию на основе `APP_RUN_ENV` (`local` → директории репозитория, `docker` → volume в контейнере вроде `/var/app/state`, `/secrets`, `/app/assets/images`).
- В `selectors` конфигов сайтов теперь задаются не только основные CSS, но и `hover_targets` (для категорий) и `product_hover_targets` (для карточек) — списки элементов, куда нужно наводить курсор для поведенческого слоя.
- `app.crawler` — движки обхода (HTTP и Playwright), поведенческий слой имитации пользователя, пагинация и дедуп на уровне запуска.
- `app.sheets` — клиент Google Sheets (OAuth2, batchUpdate, вкладки `_state` и `_runs`, поддержка доменной импёрсонации сервисного аккаунта через `GOOGLE_OAUTH_IMPERSONATED_USER`, автосоздание строк заголовков вкладок (`source_site`, `category`, `category_url`, `product_url`, …, `llm_raw`). Каждая карточка отправляется в таблицу сразу после обработки; при ошибке запись повторяется: сначала через 10 минут, затем через 20 минут (третья попытка считается последней), чтобы переждать длительные DNS/сетевые сбои.
- `app.state` — локальное хранилище (SQLite/JSONL) и синхронизация со скрытой вкладкой `_state`.
- `app.logger` — единая точка настройки Rich-логов, теперь дополнительно подключает файловый обработчик, если задан `LOG_FILE_PATH` (используемый по умолчанию путь `/var/log/parser/parser.log` пробрасывается из каталога `./logs`).
- `app.crawler.engines.ProxyPool` — управляет ротацией прокси и прямых подключений. Если один и тот же источник (прокси или прямой IP) получил два ответа 403 подряд, он попадает в файл `NETWORK_BAD_PROXY_LOG_PATH` (по умолчанию `/var/log/parser/bad_proxies.log`) и исключается из пула.
- `app.crawler.engines.BrowserEngine` — помимо быстрых ретраев (их длительность берётся из `NETWORK_RETRY_BACKOFF_SEC`, по умолчанию это 30 и 60 секунд) добавляет длинные повторы: спустя 2 и 4 минуты выполняются дополнительные попытки загрузки страницы через новый прокси/прямой IP. Это повышает шанс пройти временные блокировки страниц с глубокой пагинацией.
- `app.crawler.site_crawler.SiteCrawler` поддерживает ограничение диапазона страниц: поля `pagination.start_page` и `pagination.end_page` позволяют начать обход с произвольного номера и завершить после обработки указанной страницы (при включённом resume стартовый номер берётся как максимум из `start_page` и сохранённого прогресса).
- `scripts/prepare_runtime_dirs.py` — вспомогательный скрипт, который создаёт каталоги `state`, `assets/images`, `logs` рядом с проектом. Его запускают перед деплоем/пробросом volume, чтобы гарантировать наличие пустых директорий, которые Git не хранит.

## 2.1 Управление конфигурациями
- `app.config.loader.load_global_config` читает YAML/JSON с общими параметрами **или** строит объект `GlobalConfig` из переменных окружения (блоки `SHEET_*`, `RUNTIME_*`, `NETWORK_*`, `DEDUPE_*`, `STATE_*`).
- `app.config.loader.iter_site_configs` собирает все файлы сайта (`*.yml`, `*.yaml`, `*.json`) из каталога, валидирует их через Pydantic.
- Модели (`app.config.models`) описывают SheetConfig/Runtime/Network/Dedupe/State, а также SiteConfig с wait/stop conditions и лимитами.
- CLI принимает `--resume`, `--reset-state`, `--dry-run`, параметры путей можно передать через `.env` (`SITE_CONFIG_DIR`, `GLOBAL_CONFIG_PATH`).
- Переменная `PRODUCT_IMAGE_DIR` задаёт каталог, где складываются изображения товаров; путь передаётся в `RuntimeContext`.

## 2.2 Локальное состояние
- `app.state.storage.StateStore` создаёт SQLite-базу (`state/runtime.db` при запуске локально и `/var/app/state/runtime.db` в контейнере — путь выбирается через `app.config.runtime_paths`) и таблицу `category_state`.
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
  scroll_min_percent: 5
  scroll_max_percent: 35
  start_page: 1
  end_page: 10
limits:
  max_products: 2000
  max_scrolls: 30
category_urls:
  - "https://alcoplaza.ru/catalog/vodka/"
```
Селекторы для цен являются настраиваемыми: `price_with_discount_selector` может быть строкой или списком — в последнем случае агент проверяет каждый CSS-селектор по порядку, пока не найдёт цену со скидкой (это полезно, когда у сайта несколько вариантов вёрстки).

## 5. Контейнеризация
- Docker образ на базе `python:3.12-slim`, установка зависимостей из `requirements.txt`.
- Запуск только через `python -m app.main`.
- Конфиги монтируются в `/app/config`, состояние и credentials — в `/var/app/state` и `/secrets`. Флаг `APP_RUN_ENV=docker` заставляет рантайм автоматически использовать эти пути по умолчанию (если соответствующие переменные окружения пустые).

## 6. Файл `.env`
Используется для передачи путей к конфигурациям, state и OAuth-файлам. Все переменные снабжены комментариями с описанием источников доступа. Переменная `APP_RUN_ENV` управляет тем, какие значения будут подставлены по умолчанию (локально — каталоги репозитория, внутри контейнера — примонтированные volume `/app/config/sites`, `/app/assets/images`, `/var/app/state`, `/secrets`). Переменная `WRITE_FLUSH_PRODUCT_INTERVAL` определяет, как часто (в товарах) агент будет отправлять накопленные данные в Google Sheets (по умолчанию 1, то есть сразу после обработки записи; для обратной совместимости поддерживается `WRITE_FLUSH_PAGE_INTERVAL`). Для управления уровнем логирования CLI добавлена переменная `LOG_LEVEL`, которая пробрасывается в `app.cli` и позволяет переключать DEBUG/INFO без правки docker-команды. Дополнительно `LOG_FILE_PATH` задаёт путь к локальному файлу логов (по умолчанию `logs/parser.log` локально и `/var/log/parser/parser.log` в контейнере), поэтому можно анализировать историю запусков без `docker compose logs`. Новая переменная `NETWORK_BAD_PROXY_LOG_PATH` хранит список прокси/прямых IP, которые дважды получили ответ HTTP 403; после записи в этот файл агент исключает источник из дальнейшего использования. Параметр `NETWORK_RETRY_BACKOFF_SEC` управляет длительностью быстрых повторов как для HTTP, так и для Playwright (по умолчанию 30 и 60 секунд). Поля `pagination.start_page`, `pagination.end_page`, `pagination.scroll_min_percent` и `pagination.scroll_max_percent` задаются в конфиге сайта и позволяют управлять диапазоном страниц и глубиной скролла без правки state.

## 7. Следующие этапы
По завершении каждого этапа (config/state/crawler/sheets/надёжность) этот документ будет дополняться деталями реализации и диаграммами потоков.

## 8. Этап 3 — модуль обхода
- `app.crawler.engines` реализует `HttpEngine` (httpx + ретраи) и `BrowserEngine` (Playwright sync API, скролл для infinite_scroll). Общий интерфейс `EngineRequest`. Прокси берутся из `NETWORK_PROXY_POOL`, но при включённом `NETWORK_PROXY_ALLOW_DIRECT` движки добавляют к ротации и прямое подключение через текущую сеть, что позволяет чередовать прокси и “чистый” IP сервера.
- HTTP-вызовы (`HttpEngine`, `_fetch_html_http` в `ProductContentFetcher` и `ImageSaver`) берут готовые httpx-клиенты из фабрики `HttpClientFactory`, которая кеширует экземпляры на уровне прокси. Это устраняет передачу неподдерживаемого аргумента `proxies` в `Client.get` и даёт единообразную ротацию соединений.
- Если один и тот же прокси или прямой IP дважды подряд приводит к ответу 403, `ProxyPool` помечает источник как испорченный, записывает строку вида `<timestamp>\t<proxy>\tHTTP 403` в `NETWORK_BAD_PROXY_LOG_PATH` и перестаёт использовать его. Это относится как к загрузкам категорий/товаров, так и к скачиванию изображений.
- `app.crawler.site_crawler.SiteCrawler` поддерживает все три режима пагинации, wait/stop-conditions, счётчики, дедуп, обновление `StateStore`, а также умеет отдавать данные порциями каждыми `WRITE_FLUSH_PRODUCT_INTERVAL` товаров (по умолчанию после каждой записи, что мгновенно отправляет данные в Google Sheets и сохраняет изображение). Карточки, упавшие при загрузке/сохранении, пропускаются, URL и текст ошибки пишутся в `state/skipped_products.log`, чтобы не останавливать обход.
- Паузы между страницами категорий и карточками конфигурируются через `.env` (`RUNTIME_PAGE_DELAY_*`, `RUNTIME_PRODUCT_DELAY_*`). Для каждого запроса применяется рандомный джиттер внутри указанного диапазона, что снижает риск блокировок IP.
- `BrowserEngine` может загружать ранее экспортированный `storage_state` (cookies, localStorage) — путь задаётся через `NETWORK_BROWSER_STORAGE_STATE_PATH`. Это позволяет запускать обход от имени существующей пользовательской сессии и обходить антиботы, требующие авторизации. Дополнительно браузерный движок подключает слой `HumanBehaviorController`, который перед чтением HTML выполняет “человеческие” действия (скролл, движения мыши, hover, открытие дополнительных карточек в новых вкладках, возвраты `back/forward`) с конфигурируемыми задержками и лимитами. Поведение активируется только для `engine=browser`, а сведения (URL, прокси, действия, время) пишутся в логи. Для отладки можно отключить headless-режим (`NETWORK_BROWSER_HEADLESS=false`), чтобы видеть окно Playwright, управлять скоростью выполнений через `NETWORK_BROWSER_SLOW_MO_MS` (slow-mo Playwright), вставить паузу перед стартом поведенческого слоя (`NETWORK_BROWSER_PREVIEW_BEFORE_BEHAVIOR_SEC`), удерживать дополнительные вкладки (`NETWORK_BROWSER_EXTRA_PAGE_PREVIEW_SEC`) и оставлять основную вкладку открытой на заданное число секунд перед закрытием (`NETWORK_BROWSER_PREVIEW_DELAY_SEC`), чтобы наблюдать действия агента.
- `app.crawler.service.CrawlService` поочерёдно запускает `SiteCrawler` для каждого сайта и возвращает список `SiteCrawlResult`.
- `app.crawler.content_fetcher.ProductContentFetcher` умеет работать в двух режимах: `http` (быстрый httpx, как раньше) и `browser`, который использует Playwright + поведенческий слой для каждой карточки (включается через `PRODUCT_FETCH_ENGINE=browser`). В обоих случаях после получения HTML извлекается текст и ссылка на изображение, а скачивание файлов выполняет `app.media.image_saver.ImageSaver` сразу после обработки каждой карточки (что обеспечивает мгновенное появление изображений в `PRODUCT_IMAGE_DIR`). ImageSaver определяет расширение по заголовку `Content-Type`, поэтому ссылки с `image/webp` или `image/avif` сохраняются без принудительного преобразования в JPEG.
- Нормализация ссылок и md5-хэш находятся в `app.crawler.utils.normalize_url`.

## 9. Этап 4 — запись в Google Sheets
- `app.sheets.client.GoogleSheetsClient` инкапсулирует OAuth2 (InstalledAppFlow), проверку/создание вкладок, batchUpdate и повторные попытки с экспоненциальным бэкоффом.
- `app.sheets.writer.SheetsWriter` превращает `ProductRecord` в строки (колонки A–L), добавляя очищенный контент и путь к изображению рядом с URL товара, и умеет дозаписывать данные порциями по мере обхода (кэшируя уже существующие URL, чтобы избежать дубликатов).
- Вкладки `_runs` и `_state` создаются автоматически; `_runs` получает итоги (run_id, site, started/finished, totals), `_state` отражает содержимое SQLite-хранилища для возобновляемости.
- `AgentRunner` вызывает SheetsWriter после краулера (если не указан dry-run), сохраняя список последних результатов внутри раннера.
- Потоки записи работают по принципу «один товар — одна запись»: для каждого продукта `SiteCrawler` сразу после заполнения `ProductRecord` вызывает `SheetsWriter` (через `_queue_for_flush`). Одновременно, после каждого успешно обработанного товара происходит `upsert` в `StateStore`, поэтому прогресс (номер страницы и счётчик товаров) обновляется даже если краулер упал в середине страницы. В случае сбоя записи в таблицу выполняется до трёх попыток с паузами 10 и 20 минут между ними, после чего ошибка всплывает и изображение откатывается.

## 10. Этап 5 — надёжность и тесты
- Антиблок: ротация User-Agent/прокси, HTTP-ретраи с экспоненциальными задержками, случайный джиттер между загрузками страниц.
- Логирование через `rich` + структурированные сообщения для ключевых событий (старт/обход/запись, предупреждения по состоянию).
- Покрытие тестами (`pytest`): загрузчик конфигов, дедуп (normalize_url), state store, site crawler (пагинация и резюмируемость), SheetsWriter (моки клиента).
- Dockerfile устанавливает Playwright + системные зависимости, что гарантирует воспроизводимость внутри контейнера.

## 11. Поведенческий слой
- Конфигурация описывается в `RuntimeConfig.behavior` и настраивается через блок `BEHAVIOR_*` в `.env`/общей YAML-конфигурации: вероятности скролла, диапазоны глубин, количество движений мыши, селекторы hover и лимиты дополнительных переходов (`max_additional_chain`, `extra_products_limit`, `visit_root_probability` и т. п.).
- Селекторы для hover задаются внутри `SiteConfig.selectors.hover_targets` (категории) и `SiteConfig.selectors.product_hover_targets` (карточки), чтобы каждый сайт мог определять собственные элементы для наведения; глобальный `BehaviorMouseConfig` лишь предоставляет вероятность и числа перемещений. 
- `SiteCrawler` прокидывает в движок `BehaviorContext` (селектор товаров, URL категории, base/root URL). Это позволяет `HumanBehaviorController` выбирать реальные элементы категории и открывать дополнительные карточки в отдельных вкладках Playwright-контекста, не мешая сбору HTML.
- Для каждого прокси создаётся собственный Playwright-context с заданным `User-Agent` и `Accept-Language` (`NETWORK_ACCEPT_LANGUAGE`). Контроллер сохраняет информацию о выполненных действиях и времени пребывания на странице в логах; при включённом `BEHAVIOR_DEBUG` формат расширяется подробной телеметрией, что помогает отлаживать антиботы.
