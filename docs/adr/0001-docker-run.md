# ADR-0001: Одноразовый Docker runner

Статус: принято. Дата: 2026-10-02.

1. Один TWYLT docker_run с Input/Output, Docker SDK 7.1.0, Linux local Unix daemon.
   JSON argv, без host-shell и дополнительных сырых Docker options.
2. Host paths контролирует неизменная общая workspace-политика. Только bind,
   read-only по умолчанию. Image VOLUME требует точного bind покрытия, Mounts
   проверяются до start; named/anonymous Docker volumes запрещены.
3. Registry prefix — административная граница. Pull отсутствующего образа только
   с разрешением; запуск по immutable image ID. Pull в отдельном процессе из-за
   timeout=None в SDK; его budget отдельный от выполнения приложения.
4. Concurrency — атомарный create фиксированных имён на daemon, общий для всех
   процессов и рабочих областей. Нет local counter/flock, очереди или per-call override.
   Единая настройка N на всех workers — обязательное условие.
5. Create/start/wait/logs/remove вместо auto-remove: сохраняем exitcode и вывод,
   ошибки cleanup видны и не освобождают слот притворно.
6. Watchdog до start, отдельная session, код через python -c, identity по ID+token.
   Kill по monotonic deadline, remove после grace. Reaper по собственным labels.
   Отказ всего host/cgroup/daemon требует внешнего supervisor, не маскируем ограничение.
7. Fail-closed если daemon не подтверждает MemoryLimit/SwapLimit/CpuCfsQuota.
   Capabilities drop, no-new-privileges, pids=256, сеть none/bridge, без host network.
8. Ожидаемые отказы приложения/Docker — Output. Политические отказы — стандартная
   WorkspaceDenied с incident ID и защищённым JSONL. Не логировать argv/env/secrets.
9. Тесты уровней: unit/fake engine, SDK serialization, guardian subprocess,
   socket-backed process concurrency и opt-in реальный Docker. Не подменяем
   успешными fake-тестами утверждение о проверенном реальном Docker.
