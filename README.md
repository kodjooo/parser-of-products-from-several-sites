# Агент сбора ссылок товаров

Контейнеризованный CLI-сервис обходит категории интернет-магазинов (HTTP или Playwright), нормализует ссылки на товары и пакетно записывает их в Google Sheets (каждый домен — отдельная вкладка). Агент поддерживает возобновление с последнего состояния, дедупликацию, учёт лимитов и журналирование итогов запуска.

## Основные компоненты
- `app/cli.py` — Typer-CLI (`python -m app.main ...`).
- `app/config` — pydantic-модели и загрузчик YAML/JSON конфигов.
- `app/crawler` — движки обхода (httpx и Playwright), пагинация и дедуп.
- `app/crawler/behavior.py` — поведенческий слой Playwright (скроллы, движения мыши, дополнительные переходы и логирование действий).
- `app/state` — локальное SQLite-хранилище прогресса, синхронизируется с вкладкой `_state`.
- `app/sheets` — OAuth2 + Google Sheets API (batchUpdate, вкладки `_runs`/`_state`).
- `tests/` — pytest с моками (config/state/crawler/sheets).

## Подготовка окружения
   1. Отредактируйте `.env` (см. `.env.example`):
       - `APP_RUN_ENV` — режим запуска (`local` для прямого запуска из исходников и `docker` для контейнера). Если оставить пути (`GOOGLE_OAUTH_*`, `STATE_DATABASE_PATH`, `PRODUCT_IMAGE_DIR`, `SITE_CONFIG_DIR`, `NETWORK_BROWSER_STORAGE_STATE_PATH`) пустыми, агент подставит значения по умолчанию в зависимости от режима: `local` использует директории из репозитория (`secrets/*.json`, `state/runtime.db`, `config/sites`, `assets/images`), `docker` — внутренние каталоги контейнера (`/app/config/sites`, `/app/assets/images`, `/var/app/state`, `/secrets`).
       - блок Google (пути к JSON, токену и scopes); поддерживаются как desktop OAuth (с сохранением токена), так и service account JSON (тип `service_account`, токен не требуется);
       - для сервисного аккаунта с делегированием доменных прав задайте `GOOGLE_OAUTH_IMPERSONATED_USER` — email пользователя Google Workspace, к которому есть доступ к таблице;
       - блок `SHEET_*`, `RUNTIME_*`, `NETWORK_*`, `DEDUPE_*`, `STATE_*` — все рабочие параметры теперь задаются через `.env`;
       - `LOG_LEVEL` — уровень логирования CLI (DEBUG/INFO/WARNING/ERROR/CRITICAL), удобно менять для отладки без правки `docker-compose.yml`;
       - `SITE_CONFIG_DIR` — путь внутри контейнера, куда будет примонтирован каталог с конфигами сайтов;
       - `WRITE_FLUSH_PRODUCT_INTERVAL` — через сколько товаров отправлять накопленный буфер в Google Sheets (по умолчанию 1, запись уходит сразу после обработки товара).
       - `PRODUCT_FETCH_ENGINE` — какой движок использовать для загрузки карточек (`http` по умолчанию или `browser`, чтобы открывать каждую карточку в Playwright с поведенческим слоем).
       - `PRODUCT_IMAGE_DIR` — каталог внутри контейнера, где будут храниться скачанные изображения товаров (смонтируйте volume).
  - `NETWORK_ACCEPT_LANGUAGE` управляет Accept-Language/locale в Playwright-контекстах, `NETWORK_BROWSER_HEADLESS` позволяет включать визуальный режим Playwright (false — открыть окно Chromium), `NETWORK_BROWSER_SLOW_MO_MS` замедляет действия браузера (slow-mo Playwright), `NETWORK_BROWSER_PREVIEW_BEFORE_BEHAVIOR_SEC` даёт паузу перед стартом действий, `NETWORK_BROWSER_EXTRA_PAGE_PREVIEW_SEC` удерживает дополнительные вкладки, а `NETWORK_BROWSER_PREVIEW_DELAY_SEC` задаёт паузу перед закрытием основной вкладки (полезно, если нужно наблюдать действия браузера). `NETWORK_PROXY_ALLOW_DIRECT` даёт возможность чередовать прокси с прямыми подключениями через текущую сеть сервера. Блок переменных `BEHAVIOR_*` включает поведенческий слой (см. ниже).
2. Сформируйте конфиги сайтов `config/sites/*.yml` (selectors, pagination, limits, wait/stop conditions, список `category_urls`) и примонтируйте каталог в `SITE_CONFIG_DIR`.
   - В блоке selectors можно указать `content_drop_after` — список CSS-селекторов, после которых (включая соответствующие элементы) текст товара не попадёт в `product_content`. Это полезно для удаления блоков отзывов/рекомендаций.
   - Для дополнительных полей предусмотрите селекторы: `name_en_selector`, `name_ru_selector`, `price_without_discount_selector`, `price_with_discount_selector`, а также словарь `category_labels` (ключ — slug из URL после `/items/`, значение — человекочитаемое название категории в таблице). Для `price_with_discount_selector` можно передать список селекторов — агент пойдёт по нему сверху вниз, пока не найдёт цену.
   - Для поведенческого слоя можно указать `selectors.hover_targets` — список CSS-селекторов в категориях, куда следует плавно наводить курсор (перезаписывают глобальные настройки). Для карточек товаров добавлен отдельный список `selectors.product_hover_targets`, который позволяет задать собственные элементы (или отключить hover, передав пустой список).
   - Троттлинг запросов задаётся через `.env`: `RUNTIME_PAGE_DELAY_MIN_SEC/RUNTIME_PAGE_DELAY_MAX_SEC` — паузы между страницами категорий, `RUNTIME_PRODUCT_DELAY_MIN_SEC/RUNTIME_PRODUCT_DELAY_MAX_SEC` — паузы между загрузками карточек. Значения указываются в секундах (можно дробные) и применяются с рандомным джиттером.
   - Если сайт блокирует headless-браузер без реальных cookies, экспортируйте `storage_state` из Playwright или браузера и задайте путь в переменной `NETWORK_BROWSER_STORAGE_STATE_PATH`. Самый быстрый способ — открыть сайт в Chrome, залогиниться, затем в DevTools → Application → Storage → Cookies выгрузить cookies в JSON и сконвертировать его в формат Playwright (`npx playwright codegen --save-storage auth.json` или `python -m playwright codegen ...`). Типовой сценарий:
     1. Выполните `npx playwright codegen https://example.com --save-storage auth.json` (или `python -m playwright codegen ...`) и завершите сессию после авторизации на целевом сайте.
     2. Проверьте, что `auth.json` появился в каталоге, доступном для контейнера (например, `./secrets/auth.json`) и задайте этот путь в `.env` (`NETWORK_BROWSER_STORAGE_STATE_PATH=/secrets/auth.json`).
     3. Смонтируйте каталог с файлом в контейнер (`-v $(pwd)/secrets:/secrets`). Playwright при запуске загрузит указанное состояние и будет использовать те же cookies/localStorage.
3. Если планируете браузерный движок локально, выполните `playwright install chromium`.

## Сборка и запуск в Docker
Перед контейнерным запуском установите `APP_RUN_ENV=docker` в `.env` (или пробросьте переменную окружения), чтобы значения по умолчанию указывали на каталоги внутри контейнера (`/app/config/sites`, `/app/assets/images`, `/var/app/state`, `/secrets`).
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
2. Первый запуск запросит код авторизации в консоли; токен сохранится в `GOOGLE_OAUTH_TOKEN_PATH`. Если путь не указан явно, он определяется `APP_RUN_ENV`: локально это `state/token.json`, внутри контейнера — `/var/app/state/token.json` (аналогично `GOOGLE_OAUTH_CLIENT_SECRET_PATH` → `secrets/google-credentials.json` или `/secrets/google-credentials.json`).
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
Каждый товар записывается в таблицу сразу после обработки; при ошибке записи агент делает повторную попытку через 30 секунд (всего до двух попыток), чтобы не терять результаты из-за временных сбоев API.

## Возобновляемость и state
- Локальный SQLite (`state.runtime.db`) хранит `last_page`, `last_product_count`, `last_run_ts` на каждую категорию.
- После обхода содержимое экспортируется в скрытую вкладку `_state`, что позволяет отследить, где остановился агент.
- Для надёжности данные пишутся порциями: каждые `WRITE_FLUSH_PRODUCT_INTERVAL` товаров (по умолчанию 1, то есть после каждой записи) агент отправляет накопленные данные в Google Sheets.

## Контент и изображения товаров
- После нахождения ссылки агент переходит по URL, выгружает страницу целиком, очищает её от тегов/скриптов и записывает текст в колонку `product_content`.
- Главное изображение определяется по `og:image`, `srcset` или первому `<img>` и сохраняется сразу после обработки каждой карточки (файлы кладутся в `PRODUCT_IMAGE_DIR`, имя формируется транслитом названия товара).
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

## Локальный запуск без Docker
Если нужно воспроизвести работу агента в локальном окружении, задать `APP_RUN_ENV=local` и выполнить:
```bash
source .venv/bin/activate && set -a && source .env && set +a && python -m app.main --sites-dir config/sites
```
Команда активирует виртуальное окружение, экспортирует все переменные из `.env` и запускает агент, используя локальные директории (`config/sites`, `state`, `assets/images`, `secrets`).

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
docker compose up -d --build parser

# или только запуск (если образ уже собран)
docker compose up -d parser

# логи
docker compose logs -f parser
```

Команда автоматически подхватит `.env` из корня и смонтирует нужные volume (`config/sites`, `state`, `assets/images`, `secrets`). Каталоги должны существовать заранее.
## Поведенческий слой Playwright
- Флаг `BEHAVIOR_ENABLED` включает "человеческое" поведение для всех сайтов, у которых `engine=browser`. Контролируются случайные прокрутки, движения мыши, hover по заданным селекторам, возвраты `back/forward`, переходы на главную и открытие дополнительных карточек в фоновом окне.
- Диапазоны задержек (`BEHAVIOR_ACTION_DELAY_*`, `BEHAVIOR_SCROLL_*`, `BEHAVIOR_MOUSE_*`) задают "естественные" паузы и глубину прокрутки. Селекторы для hover задаются в конфиге сайта: `selectors.hover_targets` для категорий и `selectors.product_hover_targets` для карточек. Для отладки можно выставить `NETWORK_BROWSER_HEADLESS=false`, тогда Playwright покажет реальное окно браузера.
- Блок `BEHAVIOR_NAV_*` определяет вероятности и лимиты дополнительных переходов (сколько карточек можно открыть дополнительно, как часто делать `back`, ограничение цепочки). Переходы происходят в отдельных вкладках, основная страница категории остаётся доступной для парсинга.
- При включённом `BEHAVIOR_DEBUG=true` логируются детальные действия слоя (URL, прокси, список выполненных активностей и потраченное время) — удобно для отладки антиботов. В обычном режиме сохраняется только краткая сводка.
