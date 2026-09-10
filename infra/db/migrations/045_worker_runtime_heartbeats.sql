-- Make non-HTTP background workers observable without turning optional
-- capabilities into core API readiness dependencies. Each process owns one
-- renewable row; a monitor decides freshness from heartbeat_at rather than
-- trusting a stale "running" label after an ungraceful exit.

CREATE TABLE service_worker_heartbeats (
    worker_type TEXT NOT NULL,
    instance_id UUID NOT NULL,
    hostname TEXT NOT NULL,
    process_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    stopped_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (worker_type, instance_id),
    CONSTRAINT service_worker_heartbeats_type_check CHECK (
        worker_type ~ '^[a-z][a-z0-9_-]{1,63}$'
    ),
    CONSTRAINT service_worker_heartbeats_hostname_check CHECK (
        char_length(hostname) BETWEEN 1 AND 255
    ),
    CONSTRAINT service_worker_heartbeats_process_check CHECK (process_id > 0),
    CONSTRAINT service_worker_heartbeats_status_check CHECK (
        status IN ('starting', 'running', 'draining', 'stopped')
    ),
    CONSTRAINT service_worker_heartbeats_metadata_check CHECK (
        jsonb_typeof(metadata) = 'object'
        AND pg_column_size(metadata) <= 16384
    ),
    CONSTRAINT service_worker_heartbeats_stop_check CHECK (
        (status = 'stopped' AND stopped_at IS NOT NULL)
        OR (status <> 'stopped' AND stopped_at IS NULL)
    )
);

CREATE INDEX idx_service_worker_heartbeats_freshness
    ON service_worker_heartbeats(worker_type, heartbeat_at DESC)
    WHERE status IN ('starting', 'running', 'draining');

COMMENT ON TABLE service_worker_heartbeats IS
    'Ephemeral operational leases for background processes; freshness, not status alone, proves liveness.';
COMMENT ON COLUMN service_worker_heartbeats.metadata IS
    'Bounded operational snapshot such as durable queue depth/age and configured capability profile; never job payloads or secrets.';
