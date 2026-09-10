from typing import Any

from services.shared.worker_runtime import bounded_int_env


HNSW_EF_SEARCH = bounded_int_env("RETRIEVAL_HNSW_EF_SEARCH", 500, 40, 1_000)
HNSW_MAX_SCAN_TUPLES = bounded_int_env(
    "RETRIEVAL_HNSW_MAX_SCAN_TUPLES", 200_000, 1_000, 1_000_000
)
HNSW_SCAN_MEM_MULTIPLIER = bounded_int_env(
    "RETRIEVAL_HNSW_SCAN_MEM_MULTIPLIER", 8, 1, 64
)


def configure_filtered_hnsw(cursor: Any) -> None:
    """Apply the measured, transaction-local filtered-HNSW serving policy."""
    cursor.execute("SET LOCAL hnsw.iterative_scan = strict_order")
    cursor.execute(
        "SELECT set_config('hnsw.ef_search', %s, true)",
        (str(HNSW_EF_SEARCH),),
    )
    cursor.execute(
        "SELECT set_config('hnsw.max_scan_tuples', %s, true)",
        (str(HNSW_MAX_SCAN_TUPLES),),
    )
    cursor.execute(
        "SELECT set_config('hnsw.scan_mem_multiplier', %s, true)",
        (str(HNSW_SCAN_MEM_MULTIPLIER),),
    )
