# Testing docker 0.4.0

Python 3.12, TWYLT 1.1.1, 2026-10-08: 110 passed. 6 daemon tests skipped (Docker/Podman unavailable).
All earlier behavioral scenarios are preserved. Policy tests now use TWYLT itself;
assertions were updated for code 6 and the centralized transport incident code.
Copied source packs are scanned by builder 0.4.1 and their actual generated
launcher runs in a nested cwd outside workspace using file transport.
Filesystem and Git exercise real operations; Docker tests use mock daemon APIs.
No pack Python distribution is installed. Whole tools/shared tree is required.

Base: https://github.com/Godhart/twylt-pack-docker, commit 3d7ded8276262590885c57317351d1cc4c6d9bef.
Archive includes a patch against this fresh GitHub checkout and SHA256SUMS.

```bash
python -m pip install -r requirements.txt
# Integration tests also need toolpack-builder 0.4.1.
python -m pytest -q tests
python scripts/export_schemas.py
```

## Previous release results (historical)

# Validation — Docker TWYLT Tool 0.3.0

2026-10-02, Linux, Python 3.12, TWYLT 1.0.0, Docker SDK 7.1.0.

`python -m pytest -q`: **107 passed, 6 skipped**.

Executed:
- Docker/Podman selection, endpoint precedence, API version, log-driver options;
- Podman rootless cgroup v1 denial and v2 admission, pull/list endpoint propagation;
- network policy: accepted/invalid values, deny-before-daemon, audit code, none/bridge mapping;
- tagged image filtering, deduplication, sorting, pagination, metadata and sanitized failures;
- both standalone tools expose valid TWYLT discovery metadata;
- shared workspace and incident-log regression suite, including parallel append;
- strict inputs, registry/pull rules, resource configuration and bind validation;
- fake engine lifecycle, timeout, exit/OOM handling, output truncation and cleanup failures;
- concurrent thread calls racing for atomic daemon-name slots (peak <= configured N);
- real installed SDK serialization of CPU/RAM/swap/network/bind settings;
- describe/file transport checks without Docker;
- actual independent guardian processes using a file-backed fake Docker API,
  including SIGKILL of the calling process, timeout kill and later removal.

Skipped:
1. Cross-process test through the real SDK and a fake Unix HTTP Docker daemon:
   this execution environment raises PermissionError when creating AF_UNIX sockets.
2–6. Opt-in live Docker/Podman tests for exit/logs/cleanup, timeout, writable bind mounts,
   tagged image inventory and effective cgroup v2 limits / isolated interfaces.
   Neither Docker nor Podman is available in this environment.

These results do NOT establish real Docker/Podman cgroup enforcement, network isolation,
volume mount behavior or cleanup on a production daemon. Run the supplied live tests
on a dedicated supported Linux Docker installation before relying on this release.
The source of the shared workspace policy matches Filesystem TWYLT Pack 0.5.0.
ToolHub GUI import, Windows, macOS, Docker Desktop and remote daemons were not tested;
the latter platforms/daemon modes are outside this implementation's support.
