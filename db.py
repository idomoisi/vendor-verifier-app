"""Database layer -- reads/writes to Delta tables via Databricks SQL Connector."""

from __future__ import annotations

import json
import time
from datetime import datetime

from databricks import sql
from databricks.sdk.core import Config

RAW_VENDORS_TABLE = "`s3-write-bucket`.`ido`.raw_vendors"
VENDOR_VERIFY_TABLE = "`s3-write-bucket`.`ido`.vendor_verify"

_RAW_VENDORS_COLUMNS_ENSURED = False

_REQUIRED_COLUMNS = {
    "is_existed": "BOOLEAN",
    "jira_ticket": "STRING",
}


def _ensure_raw_vendors_columns(conn) -> None:
    """Add any columns that were introduced after the table was first created."""
    global _RAW_VENDORS_COLUMNS_ENSURED
    if _RAW_VENDORS_COLUMNS_ENSURED:
        return
    with conn.cursor() as cur:
        cur.execute(f"DESCRIBE TABLE {RAW_VENDORS_TABLE}")
        existing = {row[0] for row in cur.fetchall()}
    for col, col_type in _REQUIRED_COLUMNS.items():
        if col not in existing:
            with conn.cursor() as cur:
                cur.execute(f"ALTER TABLE {RAW_VENDORS_TABLE} ADD COLUMN {col} {col_type}")
    _RAW_VENDORS_COLUMNS_ENSURED = True


def _get_connection():
    cfg = Config()
    server = cfg.host.replace("https://", "").replace("http://", "")
    return sql.connect(
        server_hostname=server,
        http_path="/sql/1.0/warehouses/472969065f3aed02",
        credentials_provider=lambda: cfg.authenticate,
    )


def load_vendor_enum_keys() -> list[tuple[str, str]]:
    """Return list of (EnumName, DisplayValue) pairs from the Vendor enum in device.py.

    Used to populate the alias target dropdown.
    Returns empty list if GitHub is unavailable.
    """
    from github_pr import DEVICE_PY_PATH, _get_file, _get_github_token

    import re

    token = _get_github_token()
    if not token:
        return []
    try:
        content, _ = _get_file(token, DEVICE_PY_PATH)
        # Match lines like:   EnumName = "Display Value"
        # and:                 EnumName = "Display Value", VendorSource.Manuf
        pairs = re.findall(r'^\s+(\w+)\s*=\s*"([^"]+)"', content, re.MULTILINE)
        # Filter out non-vendor lines (class attributes, etc.) by excluding lowercase starts
        return [(k, v) for k, v in pairs if k[0].isupper()]
    except Exception:
        return []


def find_manuf_matches(
    display_name: str,
    threshold: float = 0.70,
    preloaded_pairs: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str, float]]:
    """Return fuzzy matches from VendorSource.Manuf entries in device.py.

    Args:
        preloaded_pairs: optional pre-fetched (enum_key, display_value) list to avoid
                         a GitHub API call. Pass cached_manuf_pairs() from app.py.

    Returns list of (enum_name, display_value, score) sorted by score desc.
    """
    from difflib import SequenceMatcher

    if preloaded_pairs is not None:
        manuf_pairs = preloaded_pairs
    else:
        from github_pr import DEVICE_PY_PATH, _get_file, _get_github_token
        import re
        token = _get_github_token()
        if not token:
            return []
        try:
            content, _ = _get_file(token, DEVICE_PY_PATH)
            manuf_pairs = re.findall(
                r'^\s+(\w+)\s*=\s*"([^"]+)",\s*VendorSource\.Manuf',
                content, re.MULTILINE
            )
        except Exception:
            return []

    name_lower = display_name.lower()
    results = []
    for enum_key, manuf_display in manuf_pairs:
        score = SequenceMatcher(None, name_lower, manuf_display.lower()).ratio()
        if score >= threshold:
            results.append((enum_key, manuf_display, score))
    results.sort(key=lambda x: x[2], reverse=True)
    return results[:5]


def is_manuf_vendor(display_name: str, preloaded_pairs: list[tuple[str, str]] | None = None) -> bool:
    """Return True if display_name has a fuzzy match in VendorSource.Manuf entries."""
    return len(find_manuf_matches(display_name, preloaded_pairs=preloaded_pairs)) > 0


def get_manuf_enum_name(display_name: str, preloaded_pairs: list[tuple[str, str]] | None = None) -> str | None:
    """Return the best-matching enum key from VendorSource.Manuf entries, or None."""
    matches = find_manuf_matches(display_name, preloaded_pairs=preloaded_pairs)
    return matches[0][0] if matches else None


def check_vendor_has_alias_entry(enum_name: str) -> bool:
    """Return True if Vendor.{enum_name} already has an entry in VENDOR_ALIASES in device.py."""
    from github_pr import DEVICE_PY_PATH, _get_file, _get_github_token

    token = _get_github_token()
    if not token:
        return False
    try:
        content, _ = _get_file(token, DEVICE_PY_PATH)
        return f"Vendor.{enum_name}:" in content
    except Exception:
        return False


def load_vendor_registry() -> list[str]:
    """Load vendor display names from device.py in the GitHub repo.

    Falls back to the silver Delta table if GitHub is unavailable.
    Also merges in previously verified normalized names from vendor_verify.
    """
    from github_pr import DEVICE_PY_PATH, REPO, _get_file, _get_github_token

    vendors: list[str] = []

    token = _get_github_token()
    if token:
        try:
            content, _ = _get_file(token, DEVICE_PY_PATH)
            # Match both:
            #   EnumName = "Display Value"
            #   EnumName = "Display Value", VendorSource.Manuf
            import re
            vendors = re.findall(r'^\s+\w+\s*=\s*"([^"]+)"', content, re.MULTILINE)
        except Exception:
            pass

    if not vendors:
        conn = _get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT vendor
                    FROM `s3-write-bucket`.`silver`.`levels_of_classification_per_vendor`
                    WHERE vendor IS NOT NULL AND TRIM(vendor) != ''
                """)
                vendors = [row[0] for row in cur.fetchall()]
        finally:
            conn.close()

    # Merge in previously AI-verified names from this app
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT DISTINCT normalized_name
                FROM {VENDOR_VERIFY_TABLE}
                WHERE normalized_name IS NOT NULL AND TRIM(normalized_name) != ''
            """)
            verified = [row[0] for row in cur.fetchall()]
    except Exception:
        verified = []
    finally:
        conn.close()

    return list(set(vendors + verified))


def load_verified_names() -> set[str]:
    """Return normalized_names from vendor_verify — used to distinguish app-verified from codebase vendors."""
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT DISTINCT normalized_name
                FROM {VENDOR_VERIFY_TABLE}
                WHERE normalized_name IS NOT NULL AND TRIM(normalized_name) != ''
            """)
            return {row[0] for row in cur.fetchall()}
    except Exception:
        return set()
    finally:
        conn.close()


def get_verified_vendor(vendor_name: str) -> dict | None:
    """Fetch the latest vendor_verify record for vendor_name (case-insensitive match).

    Returns a dict with all AI result fields, or None if not found.
    """
    import json

    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    verdict, normalized_name, website, hardware_evidence,
                    networking_proof, supported_protocols, mac_oui_check,
                    device_types, industries, analyst_note, technical_artifacts,
                    confidence_score, raw_output, search_grounded
                FROM {VENDOR_VERIFY_TABLE}
                WHERE LOWER(input_vendor) = LOWER(%(name)s)
                ORDER BY verified_at DESC
                LIMIT 1
            """, {"name": vendor_name})
            row = cur.fetchone()
            if not row:
                return None
            cols = [
                "verdict", "normalized_name", "website", "hardware_evidence",
                "networking_proof", "supported_protocols", "mac_oui_check",
                "device_types", "industries", "analyst_note", "technical_artifacts",
                "confidence_score", "raw_output", "search_grounded",
            ]
            data = dict(zip(cols, row))
            # Parse JSON list fields
            for field in ("supported_protocols", "device_types", "industries", "technical_artifacts"):
                if isinstance(data.get(field), str):
                    try:
                        data[field] = json.loads(data[field])
                    except Exception:
                        data[field] = [data[field]] if data[field] else []
            return data
    except Exception:
        return None
    finally:
        conn.close()


def upsert_raw_vendor(
    vendor_name: str,
    vendor_url: str | None,
    submitted_by: str,
    is_existed: bool,
    status: str = "PROCESSING",
    _retries: int = 3,
) -> None:
    for attempt in range(_retries):
        conn = _get_connection()
        try:
            _ensure_raw_vendors_columns(conn)
            with conn.cursor() as cur:
                cur.execute(f"""
                    MERGE INTO {RAW_VENDORS_TABLE} AS target
                    USING (
                        SELECT
                            %(vendor)s AS input_vendor,
                            %(url)s AS input_url,
                            %(status)s AS status,
                            %(submitted_by)s AS submitted_by,
                            %(is_existed)s AS is_existed,
                            current_timestamp() AS first_seen,
                            current_timestamp() AS last_seen
                    ) AS source
                    ON target.input_vendor = source.input_vendor
                    WHEN MATCHED THEN
                        UPDATE SET
                            target.input_url = source.input_url,
                            target.status = source.status,
                            target.is_existed = source.is_existed,
                            target.last_seen = source.last_seen
                    WHEN NOT MATCHED THEN
                        INSERT (input_vendor, input_url, status, submitted_by, failure_counter, is_existed, first_seen, last_seen)
                        VALUES (source.input_vendor, source.input_url, source.status, source.submitted_by, 0, source.is_existed, current_timestamp(), current_timestamp())
                """, {
                    "vendor": vendor_name,
                    "url": vendor_url,
                    "status": status,
                    "submitted_by": submitted_by,
                    "is_existed": is_existed,
                })
            return
        except Exception as e:
            if "DELTA_CONCURRENT" in str(e) and attempt < _retries - 1:
                time.sleep(1 * (attempt + 1))
            else:
                raise
        finally:
            conn.close()


def upsert_vendor_verify(vendor_name: str, result: dict, search_grounded: bool, raw_output: str) -> None:
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                MERGE INTO {VENDOR_VERIFY_TABLE} AS target
                USING (
                    SELECT
                        %(input_vendor)s AS input_vendor,
                        %(verdict)s AS verdict,
                        %(normalized_name)s AS normalized_name,
                        %(website)s AS website,
                        %(hardware_evidence)s AS hardware_evidence,
                        %(networking_proof)s AS networking_proof,
                        %(supported_protocols)s AS supported_protocols,
                        %(mac_oui_check)s AS mac_oui_check,
                        %(device_types)s AS device_types,
                        %(industries)s AS industries,
                        %(analyst_note)s AS analyst_note,
                        %(technical_artifacts)s AS technical_artifacts,
                        %(confidence_score)s AS confidence_score,
                        %(raw_output)s AS raw_output,
                        %(search_grounded)s AS search_grounded,
                        current_timestamp() AS verified_at
                ) AS source
                ON target.input_vendor = source.input_vendor
                WHEN MATCHED THEN
                    UPDATE SET
                        target.verdict = source.verdict,
                        target.normalized_name = source.normalized_name,
                        target.website = source.website,
                        target.hardware_evidence = source.hardware_evidence,
                        target.networking_proof = source.networking_proof,
                        target.supported_protocols = source.supported_protocols,
                        target.mac_oui_check = source.mac_oui_check,
                        target.device_types = source.device_types,
                        target.industries = source.industries,
                        target.analyst_note = source.analyst_note,
                        target.technical_artifacts = source.technical_artifacts,
                        target.confidence_score = source.confidence_score,
                        target.raw_output = source.raw_output,
                        target.search_grounded = source.search_grounded,
                        target.verified_at = source.verified_at
                WHEN NOT MATCHED THEN
                    INSERT *
            """, {
                "input_vendor": vendor_name,
                "verdict": result.get("verdict", "SUSPICIOUS"),
                "normalized_name": result.get("official_name", vendor_name),
                "website": result.get("website", ""),
                "hardware_evidence": result.get("hardware_evidence", ""),
                "networking_proof": result.get("networking_proof", ""),
                "supported_protocols": json.dumps(result.get("supported_protocols", [])),
                "mac_oui_check": result.get("mac_oui_check", "Unknown"),
                "device_types": json.dumps(result.get("device_types", [])),
                "industries": json.dumps(result.get("industries", [])),
                "analyst_note": result.get("analyst_note", ""),
                "technical_artifacts": json.dumps(result.get("technical_artifacts", [])),
                "confidence_score": result.get("confidence_score", ""),
                "raw_output": raw_output,
                "search_grounded": search_grounded,
            })
    finally:
        conn.close()


def update_raw_vendor_status(vendor_name: str, status: str) -> None:
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE {RAW_VENDORS_TABLE}
                SET status = %(status)s, last_seen = current_timestamp()
                WHERE input_vendor = %(vendor)s
            """, {"status": status, "vendor": vendor_name})
    finally:
        conn.close()


def set_jira_ticket(vendor_name: str, ticket_key: str) -> None:
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE {RAW_VENDORS_TABLE}
                SET jira_ticket = %(ticket)s, last_seen = current_timestamp()
                WHERE input_vendor = %(vendor)s
            """, {"ticket": ticket_key, "vendor": vendor_name})
    finally:
        conn.close()


def increment_failure(vendor_name: str) -> None:
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE {RAW_VENDORS_TABLE}
                SET
                    failure_counter = COALESCE(failure_counter, 0) + 1,
                    status = CASE WHEN COALESCE(failure_counter, 0) >= 2 THEN 'FAILED' ELSE 'PENDING' END,
                    last_seen = current_timestamp()
                WHERE input_vendor = %(vendor)s
            """, {"vendor": vendor_name})
    finally:
        conn.close()


def get_recent_verifications(limit: int = 20) -> list[dict]:
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    v.input_vendor, v.verdict, v.normalized_name, v.website,
                    v.mac_oui_check, v.search_grounded, v.verified_at,
                    r.jira_ticket
                FROM {VENDOR_VERIFY_TABLE} v
                LEFT JOIN {RAW_VENDORS_TABLE} r ON v.input_vendor = r.input_vendor
                ORDER BY v.verified_at DESC
                LIMIT {limit}
            """)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()
