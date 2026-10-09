# Docker / Podman TWYLT Tool 0.4.0

## Миграция 0.4.0

Пак основан на последней версии GitHub, проверенной 2026-10-08.
TWYLT >=1.1.1 устанавливается как библиотека; сам этот пак не собирается и
не устанавливается через pip. Установите requirements.txt и сохраняйте
каталоги tools/ и shared/docker_common/ вместе. Если новая версия TWYLT ещё не доступна
в вашем Python-индексе, сначала установите её из исходников или wheel.

Общие guardrails не копируются в инструменты: используются twylt.guardrails.
Каждый тул добавляет shared/ в sys.path относительно __file__, без зависимости
от cwd и без PYTHONPATH. Builder сканирует только tools/; его launcher требует
доступности исходного дерева. Разные версии пака запускайте в отдельных процессах.

Guardrails выключены по умолчанию. Для прежней политики задайте TWYLT_GUARDRAILS=1
и TWYLT_WORKSPACE_ROOT. TWYLT_ALLOWED_CWD опционально разрешает отдельный корень
транспорта input.json/output.json и его подкаталоги; бизнес-доступ он не расширяет.
Отказы guardrails возвращают код 6; валидация — 2, бизнес-ошибка — 5.
При выключенной политике и отсутствии workspace используются обычные пути хоста;
при указанном workspace сохраняется виртуальная семантика путей.
Механизмы TWYLT контролируют типовое использование, а произвольный Python/подпроцессы
контролируются ОС. Не рассматривайте эти проверки как файловую изоляцию.

TWYLT_DISABLE_NETWORK=1 при включённых guardrails запрещает загрузку образа и запуск с сетью. TWYLT_DOCKER_DISABLE_NETWORK сохранён как отдельный запрет сети дочернего контейнера; он не запрещает локальный API daemon. Ресурсные лимиты, registry, bind targets и очистка — бизнес-ограничения и продолжают действовать. Watchdog импортирует TWYLT, без встроенной копии guardrails.


`docker_run` запускает приложение в одноразовом Linux-контейнере, возвращает
stdout/stderr и код завершения, затем удаляет контейнер. Bind mounts разрешены
только внутри рабочей области. Общий лимит запусков действует в одном хранилище выбранного движка.

## Требования и установка

- Linux/POSIX, Python 3.10+, локальный Linux Docker Engine или Podman API service с Unix socket.
- Daemon должен поддерживать лимиты RAM, swap и CPU CFS quota; иначе отказ.
- Пользователь runner должен иметь доступ к Docker socket. SDK не использует CLI
  `docker context`: нужный Unix socket передаётся через DOCKER_HOST.
- Host-пути процесса runner и Docker daemon должны обозначать одну файловую
  систему. Remote Docker, Desktop VM и runner с иной картиной bind-paths не
  поддерживаются этой версией.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Системный Docker/Podman устанавливается отдельно; инструмент его не устанавливает и
не изменяет конфигурацию службы.

## Выбор Docker / Podman

Названия тулов `docker_run` и `docker_images` и JSON-контракты сохранены.
Все `TWYLT_DOCKER_*` политики применяются к **обоим** движкам, включая запрет
сети, pull, registry, CPU/RAM, число контейнеров и таймаут. Новых Python-зависимостей
нет: для Podman используется Docker-compatible API 1.40 через тот же SDK.
Для Docker API version согласуется автоматически.

Docker (поведение по умолчанию):

```bash
export TWYLT_CONTAINER_ENGINE=docker
export TWYLT_CONTAINER_HOST=unix:///var/run/docker.sock
```

Podman rootless, runner запущен тем же пользователем:

```bash
systemctl --user enable --now podman.socket
export TWYLT_CONTAINER_ENGINE=podman
export TWYLT_CONTAINER_HOST="unix://${XDG_RUNTIME_DIR}/podman/podman.sock"
export TWYLT_DOCKER_DISABLE_NETWORK=true
python tools/docker_images/run.py '{"limit":100}'
```

Podman rootful (runner должен иметь доступ к сокету):

```bash
sudo systemctl enable --now podman.socket
export TWYLT_CONTAINER_ENGINE=podman
export TWYLT_CONTAINER_HOST=unix:///run/podman/podman.sock
```

Если TWYLT_CONTAINER_HOST отсутствует, берётся DOCKER_HOST для Docker либо
CONTAINER_HOST для Podman. При отсутствии и этой переменной Podman использует
`/run/podman/podman.sock` для UID 0, иначе `$XDG_RUNTIME_DIR/podman/podman.sock`
(при отсутствии XDG_RUNTIME_DIR — `/run/user/<euid>/podman/podman.sock`).
Переменные другого движка игнорируются; автоматического переключения при ошибке
соединения нет. Явно заданный сокет должен принадлежать выбранному движку;
автоопределение продукта по сокету не выполняется.

Rootless Podman требует cgroup v2 с делегированными CPU/memory контроллерами.
Rootless cgroup v1 отклоняется с `docker_resource_limits_unavailable`.
Отсутствие подтверждённой поддержки CPU/RAM/swap в API также вызывает отказ.
Лимиты не отключаются автоматически. На системах с SELinux права и метки каталога
bind mounts должен настроить администратор; автоматический relabel не выполняется.

Слоты, pull, список образов и watchdog используют выбранный сокет. Все workers
одной группы должны использовать один движок, одно хранилище и одинаковый лимит.
Лимит не суммируется между Docker и Podman, отдельными rootless-пользователями
или независимыми хранилищами. При смене движка дождитесь завершения старых вызовов.

Podman получает log driver json-file (его совместимый режим) с max-size=10m,
без Docker-специфичной опции max-file. Остальные ограничения сохраняются.
Podman API service должен оставаться доступен watchdog: используйте socket
activation либо `podman system service --time=0`.

Поддержка Podman проверена на уровне конфигурации, API payload и тестовых
движков. Реальный Podman в среде сборки недоступен; перед эксплуатацией выполните
live-тесты на целевом хосте. Источники и границы совместимости: ADR 0003.

## Конфигурация runner

Все переменные в этой таблице задаются доверенной конфигурацией процесса, не
в JSON вызова. Их следует передать каждому worker ToolHub одинаково.

| Переменная | Default | Назначение |
|---|---|---|
| `TWYLT_GUARDRAILS` | `0` | Включить общую политику TWYLT; в toolhub-images включена по умолчанию |
| `TWYLT_ALLOWED_CWD` | не задан | Дополнительный корень транспорта и его подкаталогов |
| `TWYLT_DISABLE_NETWORK` | `0` | Общий запрет сети при guardrails=1, включая pull |
| `TWYLT_WORKSPACE_ROOT` | при guardrails=1 | Абсолютный физический корень разрешённых host bind mounts |
| `TWYLT_INCIDENT_LOG` | stderr | JSONL-файл вне области; рекомендуется для сохранения событий watchdog |
| `TWYLT_DOCKER_DISABLE_NETWORK` | `false` | Запретить сеть приложения: true/false или 1/0; enabled отклоняется с инцидентом |
| `TWYLT_DOCKER_ALLOW_PULL` | `false` | Разрешить скачивание отсутствующего образа: true/false или 1/0 |
| `TWYLT_DOCKER_REGISTRY` | `docker.io` | Registry с необязательным namespace: `registry.example:5000/team`, без https:// |
| `TWYLT_DOCKER_CPUS` | `1` | CPU quota на контейнер, 0.01–1024; например 0.5 или 2 |
| `TWYLT_DOCKER_MEMORY` | `512m` | RAM на контейнер; байты либо суффикс b/k/m/g, минимум 6 MiB |
| `TWYLT_DOCKER_MAX_CONTAINERS` | `2` | Суммарные слоты всех вызовов на daemon, 1–4096 |
| `TWYLT_DOCKER_MAX_TIMEOUT` | `300` | Максимальный бюджет приложения в секундах, 1–86400; также отдельный бюджет pull |
| `TWYLT_CONTAINER_ENGINE` | `docker` | Движок: docker или podman; неверное значение вызывает отказ и инцидент |
| `TWYLT_CONTAINER_HOST` | не задан | Явный локальный Unix socket; приоритет над настройками движка |
| `DOCKER_HOST` | `unix:///var/run/docker.sock` | Сокет Docker, используется только в режиме docker |
| `CONTAINER_HOST` | см. ниже | Сокет Podman, используется только в режиме podman; SSH/TCP запрещены |

Пример:

```bash
mkdir -p /srv/agent/workspace /srv/agent/logs
export TWYLT_GUARDRAILS=1
export TWYLT_WORKSPACE_ROOT=/srv/agent/workspace
export TWYLT_INCIDENT_LOG=/srv/agent/logs/docker-incidents.jsonl
export TWYLT_DOCKER_ALLOW_PULL=true
export TWYLT_DOCKER_REGISTRY=docker.io
export TWYLT_DOCKER_CPUS=1
export TWYLT_DOCKER_MEMORY=512m
export TWYLT_DOCKER_MAX_CONTAINERS=2
export TWYLT_DOCKER_MAX_TIMEOUT=300
```

Готовый пример: `config/runner.env.example`. Это пример переменных, инструмент
не загружает .env автоматически. RAM+swap ограничивается тем же значением, что
RAM: дополнительный swap контейнеру не выделяется. Суммарной CPU/RAM-квоты
поверх произведения per-container лимита на число слотов нет.

## Вызов и параметры

```bash
python tools/docker_run/run.py '{"image":"alpine:3.20","command":["echo","hello"],"timeout":10}'
python tools/docker_run/run.py '{"describe":"json_spec"}'
```

| JSON-поле | Default | Семантика |
|---|---|---|
| `image` | обязательно | Имя образа относительно заданного registry/namespace |
| `command` | null | CMD argv, непустой массив строк; null использует CMD образа |
| `entrypoint` | null | ENTRYPOINT argv; null использует образ, [] явно сбрасывает entrypoint |
| `environment` | `{}` | Строковые переменные **внутри контейнера** |
| `volumes` | `[]` | Массив host bind mounts: source, target, read_only |
| `network` | `isolated` | isolated → Docker none; enabled → bridge |
| `timeout` | min(60, MAX_TIMEOUT) | Положительные секунды, не выше административного максимума |

Оба command/entrypoint можно опустить — используется программа образа.
Если задан только entrypoint, сохраняется CMD образа как его аргументы; задайте
command явно, если требуются другие аргументы. Host shell не вызывается.
Для shell-синтаксиса явно укажите shell внутри контейнера:

```json
{
  "image": "alpine:3.20",
  "entrypoint": ["/bin/sh", "-c"],
  "command": ["printf '%s\n' \"$MESSAGE\"; ls /work"],
  "environment": {"MESSAGE": "hello"},
  "volumes": [{"source": "/project", "target": "/work", "read_only": true}],
  "network": "isolated",
  "timeout": 30
}
```

Сохраняемый результат через writable bind mount:

```json
{
  "image": "python:3.12-slim",
  "command": ["python", "/work/script.py"],
  "volumes": [{"source":"/project", "target":"/work", "read_only":false}],
  "network": "enabled",
  "timeout": 120
}
```

Путь `/project` здесь физически `/srv/agent/workspace/project`. Каталог/файл
источника должен уже существовать. Обычная writable layer контейнера удаляется;
изменения writable bind mount остаются на хосте с UID/GID процесса образа.
Пользователь процесса берётся из образа, отдельного user override нет.

## Registry и pull

При registry=docker.io `alpine:3.20` означает `docker.io/library/alpine:3.20`,
`team/tool:1` — `docker.io/team/tool:1`. При registry=registry.example/team
`tool:1` означает `registry.example/team/tool:1`.
Полное имя допустимо только внутри заданного prefix. Другой registry/namespace,
URL с credentials и image-ID как обход registry не поддерживаются.
Допустим digest вида `tool@sha256:<64 lowercase hex>`.

Сначала проверяется локальный cache. При наличии образа pull не выполняется;
ALLOW_PULL=true разрешает скачать только отсутствующий образ. Чтобы обновить
уже закэшированный изменяемый tag, обновите его вне инструмента либо используйте
immutable digest. После проверки запуск закрепляется за image ID, а не плавающим tag.
Авторизация registry — доверенный Docker config/credential helper runner;
пароли через JSON инструмента не передаются. TLS/CA и доступность registry
настраиваются в Docker, поле registry не настраивает daemon mirror.

Pull имеет отдельный wall-clock бюджет MAX_TIMEOUT. Он выполняется в дочернем
процессе, потому что Docker SDK снимает HTTP timeout для потокового pull.
По истечении бюджета клиент pull завершается; загрузка/слои на стороне daemon
могут остаться в cache. Pull выполняется до занятия контейнерного слота, поэтому
этот лимит не ограничивает число параллельных загрузок образов.

## Volumes и рабочая область

Поддержаны только bind mounts реальных host-файлов/каталогов. Named volumes и
создаваемые автоматически anonymous volumes не допускаются. Если образ содержит
Dockerfile VOLUME, каждый объявленный путь нужно покрыть **точным** bind target;
иначе отказ до запуска. После create фактические Mounts проверяются повторно.

source использует политику FS-пака: виртуальный `/`, относительные пути от корня,
запрет `..`, symlink/junction, hardlink-файлов, special files и переходов st_dev.
Дерево bind-каталога проверяется целиком. Docker socket нельзя передать как bind
source — это специальный файл. target — нормализованный абсолютный POSIX-путь
в контейнере; `/`, `/proc`, `/sys`, `/dev` и их поддеревья запрещены. Пересекающиеся
или повторные targets запрещены. Режим bind propagation — rprivate.

read_only по умолчанию true; source=/ позволяет предоставить весь разрешённый
корень. Настройки readonly/границ не устраняют TOCTOU, если другой host-процесс
может подменять деревья между проверкой и mount, и не обнаруживают bind mount
того же устройства. Политика ограничивает **host bind sources**, а не виртуальную
файловую систему внутри образа: приложение видит своё обычное container root.

## Общий лимит и очистка

Вместо локального счётчика используются фиксированные имена контейнеров:
`twylt-app-slot-0` … `twylt-app-slot-(N-1)`. Create по уникальному имени атомарен
в Docker daemon; занятый слот даёт 409, после чего пробуется следующий.
Когда свободных слотов нет, возвращается `status=busy` без запуска приложения.
Очереди и автоматического ожидания свободного слота нет.

Лимит общий для всех процессов и рабочих областей данного инструмента на одном
daemon. Чужие контейнеры не считаются. Подготовленный/завершившийся, но ещё не
удалённый контейнер тоже занимает слот: подсчёт консервативнее только running.
Не используйте эти служебные имена для сторонних контейнеров.
Все workers должны иметь одинаковый MAX_CONTAINERS. Несовпадение с существующей
lease отклоняется; менять лимит нужно после завершения всех запусков и обновления
конфигурации всех workers. Разные одновременные настройки при пустом daemon не
являются поддерживаемым режимом распределённой конфигурации.

Бюджет приложения начинается перед create и включает create/start/watchdog setup,
но не pull/inspection образа. Рабочее ожидание использует monotonic clock. При
таймауте контейнер принудительно останавливается; нормальный cleanup удаляет
контейнер и освобождает имя. --rm не используется, чтобы успеть получить exitcode
и логи. При ошибке удаления cleanup_ok=false, ok=false, status=error; контейнер
продолжает занимать слот, а watchdog и следующие вызовы повторяют очистку.

До start запускается отдельный watchdog и проверяется его ready-ответ. Он
не зависит от наличия файла tool.py и отделён от session родителя. По deadline
он убивает ещё работающий контейнер, затем оставляет до 15 секунд для чтения
логов основным вызовом и удаляет контейнер. Работает по immutable container ID
и проверяет run-token, поэтому повторное использование слота не затрагивается.
При временной недоступности API watchdog повторяет очистку каждые 5 секунд.
Последующие вызовы также удаляют просроченные контейнеры с собственными labels.

Если процесс погиб между create и запуском watchdog, контейнер ещё не запускался;
такой занятый слот будет очищен следующим вызовом после deadline+15 секунд.
SIGKILL всего runner cgroup/хоста способен уничтожить и watchdog. При выключенном
Docker daemon невозможно гарантировать немедленное kill/remove: это не внешний
постоянно работающий supervisor. После восстановления помогает reaper следующего
вызова. Не заявляется абсолютная гарантия удаления при отказе хоста/daemon.

## Локальные образы — docker_images

```bash
python tools/docker_images/run.py '{"offset":0,"limit":100}'
python tools/docker_images/run.py '{"describe":"json_spec"}'
```

Выводит только локальные **тегированные** образы: без `<none>:<none>`,
без образов только с digest. Одна запись на image ID, все его теги в `tags`
(без повторов, отсортированы), `size_bytes` — Size из Docker API,
`created` — дата создания UTC ISO 8601, либо null, если отсутствует.
Размер образа не равен уникальному занятому месту: слои могут быть общими.

Параметры: `offset` ≥ 0 (по умолчанию 0), `limit` 1–1000 (по умолчанию 100).
Ответ: `ok`, `images`, `total`, `offset`, `limit`, `next_offset`, `error`.
Сортировка по первому тегу, затем image ID. `total` — число тегированных образов,
а не тегов; `next_offset` равен null на последней странице. Offset за концом
возвращает пустую страницу. Между вызовами список может меняться.

Список включает образы любых registry, доступные локальному daemon. Это не
разрешает их запуск: registry policy в docker_run продолжает действовать.
Тул не скачивает образы, не создаёт контейнеры и не занимает слоты исполнения.
Пагинация ограничивает ответ; Docker API сначала возвращает полный локальный список.
Используются те же настройки подключения, корня и транспорта. Конфигурация
проверяется тем же Settings; ошибки daemon возвращаются с ok=false и без его
исходного сообщения. Describe работает без настроенного корня и без daemon.

## Сеть и дополнительные настройки

`TWYLT_DOCKER_DISABLE_NETWORK=true` принудительно запрещает доступ приложения
к сети. Запрос с `network:"enabled"` отклоняется **до подключения к daemon,
pull и создания контейнера**: TWYLT exit 5, JSONL-инцидент
`docker_network_forbidden`. Значения `true`/`1` включают запрет,
`false`/`0` выключают; регистр не учитывается. Некорректное значение даёт
`docker_configuration_invalid`. По умолчанию false для совместимости;
при этом network по-прежнему по умолчанию isolated.

Переменная задаётся в окружении runner. Поле environment в JSON задаёт переменные
внутри приложения и не может изменить эту политику. Запрет касается сети
контейнера: скачивание образов самим daemon регулирует ALLOW_PULL отдельно.
Для запрета обоих действий установите DISABLE_NETWORK=true и ALLOW_PULL=false.


isolated отключает внешнюю сеть контейнера (loopback остаётся). enabled даёт
обычный bridge с доступностью, определяемой daemon. Host network, опубликованные
порты, privileged, devices, дополнительные capabilities и arbitrary Docker flags
не предоставляются. Всегда cap_drop=ALL, no-new-privileges, pids_limit=256,
restart=no, без TTY/stdin и image healthcheck. Это может быть несовместимо с образами,
которым нужны capabilities, повышение привилегий или интерактивный ввод.

Контейнер не является защитой от уязвимостей Docker/kernel. Registry может содержать
недоверенный код; доступ к daemon и host-конфигурация остаются доверенной границей.
Лимита writable-layer диска в этой версии нет. Docker logs ограничены 10 MiB,
одним файлом; stdout и stderr ответа — максимум по 1 MiB, UTF-8 с replacement.
Флаги *_truncated отражают ограничение возвращённого вывода; Docker log rotation
могла уже удалить более старую часть вывода до чтения.

## Результат и ошибки

Ответ содержит ok, status, image, image_id, container_id, exit_code,
container_exit_code, stdout/stderr, *_truncated, timed_out, oom_killed,
cleanup_ok, duration_seconds и error. duration_seconds — весь вызов с setup/cleanup.

| status | Значение |
|---|---|
| succeeded | Приложение завершилось с 0 и cleanup успешен |
| failed | Приложение завершилось с ненулевым кодом |
| timed_out | Истёк бюджет выполнения или pull; exit_code=124 |
| busy | Все слоты заняты, контейнер не запущен |
| error | Ошибка Docker/guardian/log collection/cleanup |

container_exit_code сохраняет реальный код приложения/убитого процесса, если
он доступен; exit_code=124 нормализует таймаут. oom_killed передаёт State.OOMKilled.
**Проверяйте ok и cleanup_ok**, не только завершение TWYLT-процесса: валидный
операционный результат возвращается с exit 0 даже при failed/busy/error.
Нарушения политики — WorkspaceDenied и стандартный TWYLT exit 5; неверные
входы — exit 2. Ошибки защищённого файлового транспорта — exit 4.
Строки error не включают сырые Docker responses: они могут содержать секреты.
stdout/stderr самого приложения возвращаются как есть — программа сама может
вывести конфиденциальные данные.

## Журнал

Общая политика импортируется из `twylt.guardrails`; собственной копии workspace.py нет. Инциденты JSONL:
нарушения путей, registry, pull, timeout maximum, конфигурации ресурсов, mount
ограничений и ошибки очистки. Успешные запуски, busy, ненулевые коды приложения и
обычный runtime timeout отдельно в аудит не добавляются — они есть в результате.
Журнал не содержит environment, argv, содержимое файлов или registry credentials.

Задавайте TWYLT_INCIDENT_LOG вне области для сохранения событий guardian после
смерти родителя. Основной процесс без файла пишет события в stderr; guardian
отсоединён от родительских pipe, и его резервный stderr направлен в /dev/null,
поэтому без рабочего файлового sink его сообщения не сохраняются. Ошибки открытия
настроенного журнала отклоняют основной запрос до работы с Docker.

При включённых guardrails файловый транспорт TWYLT разрешён при cwd внутри
workspace или TWYLT_ALLOWED_CWD, включая подкаталоги. CLI JSON/stdin работают из любого cwd. Describe не требует Docker daemon
и конфигурации области. При JSONL в stderr перед TWYLT-ошибкой может быть отдельная
строка события. Полная схема инцидента — `schemas/workspace-incident.schema.json`.

## ToolHub / Toolpack-builder

Сканировать только tools/**/tool.py: docker_run и docker_images.
Не включать run.py одновременно. Каждый тул содержит свой контракт и бизнес-логику;
общие настройки импортируются из shared/docker_common, guardrails — из TWYLT.
Требуется весь tools/shared source tree. Guardian запускается отдельным Python
процессом и импортирует TWYLT; встроенной копии политики нет. Docker socket, корень
и env-настройки предоставляет runner.
GUI-импорт в ToolHub здесь не проверялся.

## Тесты и разработка

```bash
python -m pip install -r requirements-dev.txt
python scripts/export_schemas.py
python -m pytest -q
```

Обычные тесты проверяют fake engine, настоящий SDK serializer, конкурентные
потоки, журнал и реальный detached subprocess-watchdog с file-backed fake API.
`test_process_capacity.py` запускает независимые tool-процессы через реальный SDK
к имитированному Unix HTTP daemon; требует разрешённых локальных Unix sockets.
Это не проверка фактической Docker isolation/cgroups/mounts.

Opt-in реальные тесты выполняйте на отдельном тестовом daemon (они занимают
служебные слоты, используют контейнеры и writable временный bind mount):

```bash
docker pull alpine:3.20
TWYLT_DOCKER_LIVE_TESTS=1 python -m pytest -q tests/test_live_docker.py
```

Pull в этих тестах выключен: образ подготавливается вами явно. Тесты проверяют
stdout/stderr/exitcode, timeout/cleanup и сохранение результата в bind mount.
Результат текущего прогона и ограничения — TESTING.md. Решения — docs/adr.

Документация API, использованная при реализации:
- https://docs.docker.com/reference/cli/docker/container/create/
- https://docs.docker.com/engine/storage/bind-mounts/
- https://docker-py.readthedocs.io/en/stable/containers.html

Runtime закреплён на Docker SDK 7.1.0; его локальная реализация дополнительно
проверена для сериализации параметров и поведения pull.

### Live-тесты Podman

На отдельном тестовом Linux-хосте с cgroup v2, после настройки сокета и зависимостей:

```bash
podman pull docker.io/library/alpine:3.20
TWYLT_CONTAINER_ENGINE=podman TWYLT_DOCKER_LIVE_TESTS=1 python -m pytest -q tests/test_live_docker.py
```

Для rootful используйте `sudo podman pull` и сокет rootful-службы.
Тесты проверяют stdout/stderr, exit code, таймаут/очистку, запись bind mount,
список образов, фактические CPU/RAM/swap настройки cgroup v2 и только loopback
при isolated. Эта же suite запускается с TWYLT_CONTAINER_ENGINE=docker.

## Интеграция с toolhub-images

После публикации обновите Git ref пака в своём domain YAML и используйте
update: always для замены старого toolset. Сохраняйте весь tools/shared tree.
Образы должны содержать TWYLT >=1.1.1 и внешние зависимости из requirements.txt;
установка самого пака не требуется. Для filesystem обновите соответствующий
source pin в sources.lock.json images, затем пересоберите образы.
Тулы теперь используют общий транспорт и поддерживают TWYLT_ALLOWED_CWD;
legacy override TOOLHUB_RUN_ROOT внутри workspace для этих новых паков больше не нужен.
Не удаляйте собственные настройки data/workspace при обновлении domain.
