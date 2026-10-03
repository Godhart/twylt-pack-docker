# 0002: Network policy and local image inventory

Status: accepted. Date: 2026-10-02.

DISABLE_NETWORK defaults to false to preserve existing deployments. True rejects
explicit network=enabled before connecting to the daemon, with an audit event;
it does not silently rewrite requests. All accepted execution paths still use
none or bridge. Pull policy is independent of application networking.

Inventory lists local tagged images across registries, grouping by image ID.
Registry authorization remains enforced by docker_run. A single low-level Docker
list response avoids per-image inspect races; pagination follows deduplication
and sorting. Pagination is not a cursor-stable snapshot across requests and does
not limit the amount of metadata fetched from the daemon. Inventory has no pull,
container-create, or concurrency-slot side effects.
