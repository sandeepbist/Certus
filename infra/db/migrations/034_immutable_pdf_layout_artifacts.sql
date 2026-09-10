-- Add an immutable spatial authority for native-text PDFs. A layout artifact is
-- produced from the same configured PyMuPDF TextPage graph as its parsed text,
-- stored as a create-once compressed object, and projected into bounded page and
-- text-run rows for fast evidence resolution. Existing PDFs are deliberately not
-- backfilled: geometry recomputed by an unrelated parser pass would not prove the
-- historical parsed artifact.

CREATE TABLE document_layout_artifacts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_object_id UUID NOT NULL,
    parsed_artifact_id UUID NOT NULL,
    document_version_id UUID NOT NULL,
    document_id UUID NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    artifact_type VARCHAR(40) NOT NULL DEFAULT 'pdf_native_text_layout',
    storage_backend VARCHAR(20) NOT NULL DEFAULT 's3',
    bucket VARCHAR(255) NOT NULL,
    object_key TEXT NOT NULL,
    object_version_id TEXT NOT NULL,
    mime_type VARCHAR(100) NOT NULL
        DEFAULT 'application/vnd.certus.pdf-layout+json',
    content_encoding VARCHAR(20) NOT NULL DEFAULT 'gzip',
    byte_length BIGINT NOT NULL,
    uncompressed_byte_length BIGINT NOT NULL,
    content_sha256 VARCHAR(64) NOT NULL,
    canonical_content_sha256 VARCHAR(64) NOT NULL,
    checksum_sha256_base64 VARCHAR(44) NOT NULL,
    etag TEXT,
    storage_class VARCHAR(64),
    server_side_encryption VARCHAR(64),
    kms_key_id TEXT,
    bucket_key_enabled BOOLEAN,
    layout_schema_version INT NOT NULL,
    producer_profile JSONB NOT NULL,
    coordinate_system VARCHAR(80) NOT NULL,
    page_join_contract VARCHAR(80) NOT NULL,
    page_count INT NOT NULL,
    text_run_count INT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'ready',
    last_error TEXT,
    stored_at TIMESTAMPTZ NOT NULL,
    last_verified_at TIMESTAMPTZ,
    delete_requested_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT document_layout_artifacts_source_scope_fk FOREIGN KEY (
        source_object_id, document_version_id, document_id, tenant_id, user_id
    ) REFERENCES document_source_objects(
        id, document_version_id, document_id, tenant_id, user_id
    ) ON DELETE CASCADE,
    CONSTRAINT document_layout_artifacts_parsed_scope_fk FOREIGN KEY (
        parsed_artifact_id, document_version_id, document_id, tenant_id, user_id
    ) REFERENCES document_parsed_artifacts(
        id, document_version_id, document_id, tenant_id, user_id
    ) ON DELETE CASCADE,
    CONSTRAINT document_layout_artifacts_type_check CHECK (
        artifact_type = 'pdf_native_text_layout'
    ),
    CONSTRAINT document_layout_artifacts_storage_check CHECK (
        storage_backend = 's3'
        AND content_encoding = 'gzip'
        AND mime_type = 'application/vnd.certus.pdf-layout+json'
    ),
    CONSTRAINT document_layout_artifacts_hash_check CHECK (
        content_sha256 ~ '^[0-9a-f]{64}$'
        AND canonical_content_sha256 ~ '^[0-9a-f]{64}$'
        AND checksum_sha256_base64 ~ '^[A-Za-z0-9+/]{43}=$'
    ),
    CONSTRAINT document_layout_artifacts_size_check CHECK (
        byte_length > 0 AND uncompressed_byte_length > 0
    ),
    CONSTRAINT document_layout_artifacts_profile_check CHECK (
        layout_schema_version = 1
        AND coordinate_system = 'pymupdf_unrotated_cropbox_top_left_points:v1'
        AND page_join_contract = 'physical_pages_form_feed:v1'
        AND jsonb_typeof(producer_profile) = 'object'
        AND producer_profile @> '{"schema_version": 1}'::jsonb
    ),
    CONSTRAINT document_layout_artifacts_counts_check CHECK (
        page_count > 0 AND text_run_count >= 0
    ),
    CONSTRAINT document_layout_artifacts_status_check CHECK (
        status IN ('ready', 'missing', 'delete_pending', 'deleted', 'error')
    ),
    CONSTRAINT document_layout_artifacts_deleted_state_check CHECK (
        (status = 'deleted' AND deleted_at IS NOT NULL)
        OR (status <> 'deleted' AND deleted_at IS NULL)
    ),
    UNIQUE(parsed_artifact_id),
    UNIQUE(bucket, object_key),
    UNIQUE(id, parsed_artifact_id, document_version_id, document_id, tenant_id, user_id)
);

CREATE INDEX idx_document_layout_artifacts_scope
    ON document_layout_artifacts(tenant_id, user_id, document_id, document_version_id);

CREATE INDEX idx_document_layout_artifacts_lifecycle
    ON document_layout_artifacts(status, delete_requested_at, created_at);

CREATE TABLE document_layout_pages (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    layout_artifact_id UUID NOT NULL,
    parsed_artifact_id UUID NOT NULL,
    document_version_id UUID NOT NULL,
    document_id UUID NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    page_index INT NOT NULL,
    page_label TEXT NOT NULL,
    width_points DOUBLE PRECISION NOT NULL,
    height_points DOUBLE PRECISION NOT NULL,
    rotation_degrees SMALLINT NOT NULL,
    media_x0 DOUBLE PRECISION NOT NULL,
    media_y0 DOUBLE PRECISION NOT NULL,
    media_x1 DOUBLE PRECISION NOT NULL,
    media_y1 DOUBLE PRECISION NOT NULL,
    crop_x0 DOUBLE PRECISION NOT NULL,
    crop_y0 DOUBLE PRECISION NOT NULL,
    crop_x1 DOUBLE PRECISION NOT NULL,
    crop_y1 DOUBLE PRECISION NOT NULL,
    parsed_start INT NOT NULL,
    parsed_end INT NOT NULL,
    extraction_status VARCHAR(24) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT document_layout_pages_artifact_scope_fk FOREIGN KEY (
        layout_artifact_id, parsed_artifact_id, document_version_id,
        document_id, tenant_id, user_id
    ) REFERENCES document_layout_artifacts(
        id, parsed_artifact_id, document_version_id, document_id, tenant_id, user_id
    ) ON DELETE CASCADE,
    CONSTRAINT document_layout_pages_index_check CHECK (page_index >= 0),
    CONSTRAINT document_layout_pages_label_check CHECK (page_label <> ''),
    CONSTRAINT document_layout_pages_dimensions_check CHECK (
        width_points > 0 AND height_points > 0
        AND media_x1 > media_x0 AND media_y1 > media_y0
        AND crop_x1 > crop_x0 AND crop_y1 > crop_y0
        AND abs(width_points - (crop_x1 - crop_x0)) < 0.01
        AND abs(height_points - (crop_y1 - crop_y0)) < 0.01
        AND array_position(
            ARRAY[
                width_points, height_points,
                media_x0, media_y0, media_x1, media_y1,
                crop_x0, crop_y0, crop_x1, crop_y1
            ],
            'NaN'::double precision
        ) IS NULL
        AND 'Infinity'::double precision <> ALL(ARRAY[
            width_points, height_points,
            media_x0, media_y0, media_x1, media_y1,
            crop_x0, crop_y0, crop_x1, crop_y1
        ])
        AND '-Infinity'::double precision <> ALL(ARRAY[
            width_points, height_points,
            media_x0, media_y0, media_x1, media_y1,
            crop_x0, crop_y0, crop_x1, crop_y1
        ])
    ),
    CONSTRAINT document_layout_pages_rotation_check CHECK (
        rotation_degrees IN (0, 90, 180, 270)
    ),
    CONSTRAINT document_layout_pages_range_check CHECK (
        parsed_start >= 0 AND parsed_end >= parsed_start
    ),
    CONSTRAINT document_layout_pages_extraction_check CHECK (
        extraction_status IN ('native_text', 'empty')
        AND (
            (extraction_status = 'native_text' AND parsed_end > parsed_start)
            OR (extraction_status = 'empty' AND parsed_end = parsed_start)
        )
    ),
    UNIQUE(layout_artifact_id, page_index),
    UNIQUE(id, layout_artifact_id, page_index)
);

CREATE INDEX idx_document_layout_pages_scope
    ON document_layout_pages(tenant_id, user_id, document_id, page_index);

CREATE TABLE document_text_runs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    layout_artifact_id UUID NOT NULL,
    layout_page_id UUID NOT NULL,
    page_index INT NOT NULL,
    parsed_start INT NOT NULL,
    parsed_end INT NOT NULL,
    reading_order INT NOT NULL,
    source_block_index INT NOT NULL,
    line_index INT NOT NULL,
    span_index INT NOT NULL,
    text_sha256 VARCHAR(64) NOT NULL,
    bbox_x0 DOUBLE PRECISION NOT NULL,
    bbox_y0 DOUBLE PRECISION NOT NULL,
    bbox_x1 DOUBLE PRECISION NOT NULL,
    bbox_y1 DOUBLE PRECISION NOT NULL,
    quad_ul_x DOUBLE PRECISION NOT NULL,
    quad_ul_y DOUBLE PRECISION NOT NULL,
    quad_ur_x DOUBLE PRECISION NOT NULL,
    quad_ur_y DOUBLE PRECISION NOT NULL,
    quad_ll_x DOUBLE PRECISION NOT NULL,
    quad_ll_y DOUBLE PRECISION NOT NULL,
    quad_lr_x DOUBLE PRECISION NOT NULL,
    quad_lr_y DOUBLE PRECISION NOT NULL,
    direction_x DOUBLE PRECISION NOT NULL,
    direction_y DOUBLE PRECISION NOT NULL,
    writing_mode SMALLINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT document_text_runs_page_fk FOREIGN KEY (
        layout_page_id, layout_artifact_id, page_index
    ) REFERENCES document_layout_pages(id, layout_artifact_id, page_index)
      ON DELETE CASCADE,
    CONSTRAINT document_text_runs_range_check CHECK (
        parsed_start >= 0 AND parsed_end > parsed_start
    ),
    CONSTRAINT document_text_runs_order_check CHECK (
        reading_order >= 0 AND source_block_index >= 0
        AND line_index >= 0 AND span_index >= 0
    ),
    CONSTRAINT document_text_runs_hash_check CHECK (
        text_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT document_text_runs_bbox_check CHECK (
        bbox_x1 >= bbox_x0 AND bbox_y1 >= bbox_y0
        AND array_position(
            ARRAY[
                bbox_x0, bbox_y0, bbox_x1, bbox_y1,
                quad_ul_x, quad_ul_y, quad_ur_x, quad_ur_y,
                quad_ll_x, quad_ll_y, quad_lr_x, quad_lr_y,
                direction_x, direction_y
            ],
            'NaN'::double precision
        ) IS NULL
        AND 'Infinity'::double precision <> ALL(ARRAY[
            bbox_x0, bbox_y0, bbox_x1, bbox_y1,
            quad_ul_x, quad_ul_y, quad_ur_x, quad_ur_y,
            quad_ll_x, quad_ll_y, quad_lr_x, quad_lr_y,
            direction_x, direction_y
        ])
        AND '-Infinity'::double precision <> ALL(ARRAY[
            bbox_x0, bbox_y0, bbox_x1, bbox_y1,
            quad_ul_x, quad_ul_y, quad_ur_x, quad_ur_y,
            quad_ll_x, quad_ll_y, quad_lr_x, quad_lr_y,
            direction_x, direction_y
        ])
        AND abs(sqrt(direction_x * direction_x + direction_y * direction_y) - 1) < 0.001
    ),
    CONSTRAINT document_text_runs_writing_mode_check CHECK (writing_mode IN (0, 1)),
    UNIQUE(layout_artifact_id, page_index, reading_order)
);

CREATE INDEX idx_document_text_runs_resolve
    ON document_text_runs(layout_artifact_id, parsed_start, parsed_end);

CREATE FUNCTION validate_pdf_layout_artifact()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    parsed_text TEXT;
    actual_pages INT;
    actual_runs INT;
BEGIN
    SELECT parsed.content_text
    INTO parsed_text
    FROM document_parsed_artifacts AS parsed
    JOIN document_source_objects AS source
      ON source.id = parsed.source_object_id
     AND source.document_version_id = parsed.document_version_id
     AND source.document_id = parsed.document_id
     AND source.tenant_id = parsed.tenant_id
     AND source.user_id = parsed.user_id
    WHERE parsed.id = NEW.parsed_artifact_id
      AND parsed.status = 'ready'
      AND source.id = NEW.source_object_id
      AND source.status = 'available'
      AND split_part(lower(source.claimed_mime_type), ';', 1) = 'application/pdf';

    IF parsed_text IS NULL THEN
        RAISE EXCEPTION 'PDF layout artifact requires a ready PDF parsed artifact';
    END IF;

    SELECT COUNT(*) INTO actual_pages
    FROM document_layout_pages
    WHERE layout_artifact_id = NEW.id;

    SELECT COUNT(*) INTO actual_runs
    FROM document_text_runs
    WHERE layout_artifact_id = NEW.id;

    IF actual_pages <> NEW.page_count OR actual_runs <> NEW.text_run_count THEN
        RAISE EXCEPTION 'PDF layout projection counts do not match the artifact catalog';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM (
            SELECT page.*,
                   lag(parsed_end) OVER (ORDER BY page_index) AS prior_end
            FROM document_layout_pages AS page
            WHERE layout_artifact_id = NEW.id
        ) AS ordered_page
        WHERE page_index < 0
           OR page_index >= NEW.page_count
           OR (page_index = 0 AND parsed_start <> 0)
           OR (page_index > 0 AND parsed_start <> prior_end + 1)
           OR parsed_end > char_length(parsed_text)
           OR (
               page_index > 0
               AND substring(parsed_text FROM prior_end + 1 FOR 1) <> chr(12)
           )
    ) OR NOT EXISTS (
        SELECT 1
        FROM document_layout_pages
        WHERE layout_artifact_id = NEW.id
          AND page_index = NEW.page_count - 1
          AND parsed_end = char_length(parsed_text)
    ) THEN
        RAISE EXCEPTION 'PDF layout page ranges do not cover the parsed artifact exactly';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM document_text_runs AS run
        JOIN document_layout_pages AS page
          ON page.id = run.layout_page_id
         AND page.layout_artifact_id = run.layout_artifact_id
         AND page.page_index = run.page_index
        WHERE run.layout_artifact_id = NEW.id
          AND (
              run.parsed_start < page.parsed_start
              OR run.parsed_end > page.parsed_end
              OR encode(
                    digest(
                        convert_to(
                            substring(
                                parsed_text
                                FROM run.parsed_start + 1
                                FOR run.parsed_end - run.parsed_start
                            ),
                            'UTF8'
                        ),
                        'sha256'
                    ),
                    'hex'
                 ) <> run.text_sha256
          )
    ) THEN
        RAISE EXCEPTION 'PDF text run does not resolve exactly in the parsed artifact';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM (
            SELECT parsed_start,
                   lag(parsed_end) OVER (ORDER BY parsed_start, parsed_end, reading_order) AS prior_end
            FROM document_text_runs
            WHERE layout_artifact_id = NEW.id
        ) AS ordered_run
        WHERE prior_end IS NOT NULL AND parsed_start < prior_end
    ) THEN
        RAISE EXCEPTION 'PDF text runs overlap';
    END IF;

    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER trg_validate_pdf_layout_artifact
    AFTER INSERT ON document_layout_artifacts
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_pdf_layout_artifact();

CREATE FUNCTION protect_document_layout_artifact()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.id IS DISTINCT FROM NEW.id
       OR OLD.source_object_id IS DISTINCT FROM NEW.source_object_id
       OR OLD.parsed_artifact_id IS DISTINCT FROM NEW.parsed_artifact_id
       OR OLD.document_version_id IS DISTINCT FROM NEW.document_version_id
       OR OLD.document_id IS DISTINCT FROM NEW.document_id
       OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.user_id IS DISTINCT FROM NEW.user_id
       OR OLD.artifact_type IS DISTINCT FROM NEW.artifact_type
       OR OLD.storage_backend IS DISTINCT FROM NEW.storage_backend
       OR OLD.bucket IS DISTINCT FROM NEW.bucket
       OR OLD.object_key IS DISTINCT FROM NEW.object_key
       OR OLD.object_version_id IS DISTINCT FROM NEW.object_version_id
       OR OLD.mime_type IS DISTINCT FROM NEW.mime_type
       OR OLD.content_encoding IS DISTINCT FROM NEW.content_encoding
       OR OLD.byte_length IS DISTINCT FROM NEW.byte_length
       OR OLD.uncompressed_byte_length IS DISTINCT FROM NEW.uncompressed_byte_length
       OR OLD.content_sha256 IS DISTINCT FROM NEW.content_sha256
       OR OLD.canonical_content_sha256 IS DISTINCT FROM NEW.canonical_content_sha256
       OR OLD.checksum_sha256_base64 IS DISTINCT FROM NEW.checksum_sha256_base64
       OR OLD.etag IS DISTINCT FROM NEW.etag
       OR OLD.storage_class IS DISTINCT FROM NEW.storage_class
       OR OLD.server_side_encryption IS DISTINCT FROM NEW.server_side_encryption
       OR OLD.kms_key_id IS DISTINCT FROM NEW.kms_key_id
       OR OLD.bucket_key_enabled IS DISTINCT FROM NEW.bucket_key_enabled
       OR OLD.layout_schema_version IS DISTINCT FROM NEW.layout_schema_version
       OR OLD.producer_profile IS DISTINCT FROM NEW.producer_profile
       OR OLD.coordinate_system IS DISTINCT FROM NEW.coordinate_system
       OR OLD.page_join_contract IS DISTINCT FROM NEW.page_join_contract
       OR OLD.page_count IS DISTINCT FROM NEW.page_count
       OR OLD.text_run_count IS DISTINCT FROM NEW.text_run_count
       OR OLD.stored_at IS DISTINCT FROM NEW.stored_at
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'document layout artifact identity and content fields are immutable';
    END IF;

    IF OLD.last_verified_at IS NOT NULL
       AND (NEW.last_verified_at IS NULL OR NEW.last_verified_at < OLD.last_verified_at) THEN
        RAISE EXCEPTION 'document layout artifact verification time cannot move backwards';
    END IF;

    IF (OLD.delete_requested_at IS NOT NULL
        AND OLD.delete_requested_at IS DISTINCT FROM NEW.delete_requested_at)
       OR (OLD.deleted_at IS NOT NULL AND OLD.deleted_at IS DISTINCT FROM NEW.deleted_at) THEN
        RAISE EXCEPTION 'document layout artifact lifecycle timestamps are write-once';
    END IF;

    IF (OLD.status = 'ready' AND NEW.status NOT IN ('ready', 'missing', 'delete_pending', 'error'))
       OR (OLD.status = 'missing' AND NEW.status NOT IN ('ready', 'missing', 'delete_pending', 'error'))
       OR (OLD.status = 'error' AND NEW.status NOT IN ('ready', 'missing', 'delete_pending', 'error'))
       OR (OLD.status = 'delete_pending' AND NEW.status NOT IN ('ready', 'delete_pending', 'deleted', 'error'))
       OR (OLD.status = 'deleted' AND NEW.status <> 'deleted') THEN
        RAISE EXCEPTION 'invalid document layout artifact state transition: % -> %', OLD.status, NEW.status;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_protect_document_layout_artifact
    BEFORE UPDATE ON document_layout_artifacts
    FOR EACH ROW EXECUTE FUNCTION protect_document_layout_artifact();

CREATE FUNCTION protect_document_layout_projection()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD IS DISTINCT FROM NEW THEN
        RAISE EXCEPTION 'document layout projection rows are immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_protect_document_layout_page
    BEFORE UPDATE ON document_layout_pages
    FOR EACH ROW EXECUTE FUNCTION protect_document_layout_projection();

CREATE TRIGGER trg_protect_document_text_run
    BEFORE UPDATE ON document_text_runs
    FOR EACH ROW EXECUTE FUNCTION protect_document_layout_projection();

COMMENT ON TABLE document_layout_artifacts IS
    'Immutable compressed native-PDF layout authority produced from the same configured TextPage graph as its parsed artifact.';
COMMENT ON TABLE document_layout_pages IS
    'Physical PDF page geometry and exact half-open parsed-artifact range, including empty pages.';
COMMENT ON TABLE document_text_runs IS
    'Bounded span-level spatial projection whose text digest is deferred-constraint verified against the immutable parsed artifact.';
