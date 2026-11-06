CREATE TABLE auth_mail_outbox (
    id UUID PRIMARY KEY,
    ciphertext BYTEA,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'uncertain', 'expired')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '15 minutes',
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    CHECK ((status IN ('pending', 'sending')) = (ciphertext IS NOT NULL)),
    CHECK (octet_length(ciphertext) <= 32768)
);
CREATE INDEX auth_mail_outbox_pending ON auth_mail_outbox(created_at) WHERE status = 'pending';
REVOKE ALL ON auth_mail_outbox FROM PUBLIC;
COMMENT ON TABLE auth_mail_outbox IS
    'Private encrypted auth mail only. No tenant payloads. One SMTP attempt; interrupted attempts are uncertain and never automatically retried.';
