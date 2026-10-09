# 0.4.0 — 2026-10-08

- Replace copied workspace/transport checks with TWYLT >=1.1.1 APIs.
- Move shared business helpers into shared/docker_common; keep tool-only code with its tool.
- Remove source-copy generation; deploy source tree without building a Python pack.
- Support optional nested transport cwd outside workspace and common network policy.
- Preserve existing scenarios, add actual builder launcher checks, refresh schemas and ADR.

# Changelog

## 0.3.0 — 2026-10-02

- Выбор docker/podman через TWYLT_CONTAINER_ENGINE и явный TWYLT_CONTAINER_HOST.
- Podman socket defaults для rootful/rootless; Docker-compatible API 1.40.
- Общие политики и watchdog, адаптация log options; запрет rootless cgroup v1.
- Сохранены имена тулов и контракты; добавлены backend и live-тесты.

## 0.2.0 — 2026-10-02

- TWYLT_DOCKER_DISABLE_NETWORK: fail-closed проверка и инцидент при попытке включить сеть.
- docker_images: локальные тегированные образы, метаданные, сортировка и пагинация.
- Два standalone entrypoint, схемы, примеры и регрессионные тесты.

## 0.1.0 — 2026-10-02

Первый выпуск docker_run: одноразовые контейнеры, ограниченные bind mounts,
ресурсы и таймаут, registry/pull policy, общие слоты, guardian и журнал.
