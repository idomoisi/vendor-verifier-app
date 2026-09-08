# Databricks notebook source

# MAGIC %md
# MAGIC # Coralogix → Vendor Verifier Batch Pipeline
# MAGIC
# MAGIC **What this does:**
# MAGIC Pulls unknown CTD vendor strings from Coralogix logs, deduplicates against the existing
# MAGIC vendor registry, and runs each new candidate through the Gemini Pro + Google Search
# MAGIC verification flow.
# MAGIC
# MAGIC Results are stored in a dedicated `coralogix_vendor_candidates` table — separate from
# MAGIC the interactive single-vendor `raw_vendors` / `vendor_verify` tables.
# MAGIC
# MAGIC **Pipeline stages:**
# MAGIC 1. `INIT` — generate run ID, create tables, define helpers
# MAGIC 2. `EXTRACT` — unified Coralogix DataPrime (ctd + integration + lansweeper)
# MAGIC 3. `FILTER` — skip COMPLETED / DUPLICATE / **FAILED**; apply orgs_count + cost cap
# MAGIC 4. `VERIFY` — Gemini Pro + duplicate gates per vendor (retry + rate limiting)
# MAGIC 5. `REPORT` — summary display + run log
# MAGIC 6. `CREATE_TICKETS` — **one** batch Jira NET ticket for the whole run
# MAGIC 7. `CREATE_PR` — optional batched GitHub PR (`CREATE_PR=True`)
# MAGIC
# MAGIC **Batch-only:** this pipeline never creates a per-vendor NET ticket or a single-vendor
# MAGIC PR. Each run produces at most one batch ticket and one batch PR, and only when at
# MAGIC least `MIN_BATCH_SIZE` (>= 2) vendors qualify — otherwise the rows stay pending and
# MAGIC roll into the next run. Single-vendor tickets/PRs belong to the interactive
# MAGIC Device Labeler Vendor Verifier tab.
# MAGIC
# MAGIC **Execution plan:** `pipeline/README.md` in `idomoisi/vendor-verifier-app`
# MAGIC (repo SoT). Local medigator `notebooks/databricks/` copies are not the commit target.
# MAGIC
# MAGIC **Name normalization → alias:** When Gemini corrects the log string (e.g. typo or
# MAGIC official branding), `should_add_alias` is set so the **raw Coralogix string** is added
# MAGIC to `VENDOR_ALIASES` for the new vendor — otherwise production keeps logging the warning.
# MAGIC
# MAGIC **How to resume after a failure:**
# MAGIC Use **Run All** (not the Databricks "Resume" button — that only replays cells and loses all Python state).
# MAGIC Re-running from scratch is safe: the FILTER stage automatically skips vendors already
# MAGIC marked `COMPLETED`, `DUPLICATE`, or `FAILED` in `coralogix_vendor_candidates`.
# MAGIC Set `FORCE_REVERIFY = True` to reprocess (including FAILED). See execution plan for run modes.

# COMMAND ----------

# MAGIC %pip install -q --upgrade google-genai "typing_extensions>=4.12.0"

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

import datetime
import json
import re
import time
import traceback
import uuid
import sys

import pandas as pd
import requests
from pytz import timezone

# Pipeline lib (Databricks workspace) — same path as VERIFY-stage imports.
_PIPELINE_LIB = "/Workspace/Users/ido.m@claroty.com/vendor-verifier-pipeline-lib"
if _PIPELINE_LIB not in sys.path:
    sys.path.insert(0, _PIPELINE_LIB)

# ── Run mode ──────────────────────────────────────────────────────────────────
# mega_backfill: capped VERIFY for one-time / follow-up mega batches (CREATE_PR manual after review)
# weekly:        scheduled job — VERIFY cap 50, CREATE_PR automatic
dbutils.widgets.text("RUN_MODE", "mega_backfill")
RUN_MODE = dbutils.widgets.get("RUN_MODE")
CREATE_PR = False            # True → open batch PR after CREATE_TICKETS
PR_MODE = "mega"             # "mega" | "weekly" (used when CREATE_PR=True)
# Jira tickets: off by default for mega/resume — set True explicitly after reviewing Delta.
# Weekly mode turns this on. Scope defaults to this run only (prevents backlog floods).
CREATE_TICKETS = False
TICKETS_SCOPE = "current_run"  # "current_run" | "all_pending" (all_pending = full backlog)

# ── Batch-only policy ─────────────────────────────────────────────────────────
# This pipeline creates ONE batch NET ticket and ONE batch PR per run — never a
# per-vendor ticket or PR. A run with fewer qualifying vendors than MIN_BATCH_SIZE
# creates nothing; the rows stay pending and roll into the next run's batch.
# Single-vendor tickets/PRs are the interactive Device Labeler path only.
MIN_BATCH_SIZE = 2  # hard floor is 2 — a batch of one is a single-vendor PR

# ── Time window ───────────────────────────────────────────────────────────────
DAYS_AGO = 90
CORALOGIX_TIER = "TIER_ARCHIVE"   # TIER_ARCHIVE for >14 days, TIER_FREQUENT_SEARCH for recent
EXTRACT_LIMIT_PER_CHANNEL = 500

# ── Vendor filter ─────────────────────────────────────────────────────────────
MIN_ORGS_COUNT = 1        # Only process vendors seen in at least N orgs
FORCE_REVERIFY = False    # True = re-verify even if already COMPLETED / DUPLICATE / FAILED

if RUN_MODE == "mega_backfill":
    # Second mega (Aug 2026): top 80 by orgs/events after skipping COMPLETED/DUPLICATE/FAILED.
    # Set 0 only if you intentionally want unlimited VERIFY.
    MAX_VENDORS_PER_RUN = 80
elif RUN_MODE == "weekly":
    MAX_VENDORS_PER_RUN = 50
    CREATE_TICKETS = True
    CREATE_PR = True
    PR_MODE = "weekly"
else:
    MAX_VENDORS_PER_RUN = 20

SKIP_STATUSES = () if FORCE_REVERIFY else ("COMPLETED", "DUPLICATE", "FAILED")
AUTO_PR_CONFIDENCES = {"HIGH", "MEDIUM"}

if MIN_BATCH_SIZE < 2:
    raise ValueError(
        f"MIN_BATCH_SIZE={MIN_BATCH_SIZE} is not allowed — the Coralogix pipeline is "
        "batch-only and must never create a single-vendor ticket or PR."
    )

# ── Duplicate gate thresholds ─────────────────────────────────────────────────
SIMILARITY_THRESHOLD = 0.70   # Minimum score to surface as "similar" in logs
DUPLICATE_THRESHOLD  = 0.90   # Minimum score to classify as a duplicate

# ── Schema / tables ───────────────────────────────────────────────────────────
CATALOG = "`s3-write-bucket`.`ido`"
CANDIDATES_TABLE    = f"{CATALOG}.coralogix_vendor_candidates"   # new dedicated table
PIPELINE_RUNS_TABLE = f"{CATALOG}.coralogix_pipeline_runs"

# Vendor registry source (read-only — not written to by this pipeline)
SILVER_VENDORS_TABLE = "`s3-write-bucket`.`silver`.`levels_of_classification_per_vendor`"
VERIFY_TABLE_READONLY = f"{CATALOG}.vendor_verify"   # for registry enrichment only

# ── Secrets ───────────────────────────────────────────────────────────────────
CORALOGIX_SECRET_SCOPE = "vendor-validation-app"
CORALOGIX_SECRET_KEY   = "coralogix_api_key"
GEMINI_SECRET_SCOPE    = "gemini_api_key"
GEMINI_SECRET_KEY      = "api_key"

CORALOGIX_ENDPOINT = "https://ng-api-http.coralogix.com/api/v1/dataprime/query"
GEMINI_MODEL_ID    = "gemini-3.6-flash"
GEMINI_MAX_RETRIES = 3
# Stop VERIFY after N consecutive Gemini/API failures (systemic outage — don't burn the whole queue)
MAX_CONSECUTIVE_GEMINI_FAILURES = 5

print("Configuration loaded.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## ⚠️ How to run / resume
# MAGIC
# MAGIC | Goal | Action |
# MAGIC |---|---|
# MAGIC | Mega / resume VERIFY only | `CREATE_TICKETS=False`, `CREATE_PR=False`, **Run All** |
# MAGIC | Batch ticket for this run only | `CREATE_TICKETS=True`, `TICKETS_SCOPE=current_run` |
# MAGIC | Batch ticket for full backlog | `CREATE_TICKETS=True`, `TICKETS_SCOPE=all_pending` (explicit) |
# MAGIC | Mega batch PR (after SQL review) | `CREATE_PR=True`, `PR_MODE=mega`, **Run All** |
# MAGIC | Weekly scheduled run | `RUN_MODE=weekly` (auto tickets + PR, current_run scope) |
# MAGIC | Resume after failure | **Run All** — skips COMPLETED/DUPLICATE/FAILED |
# MAGIC | Reprocess including FAILED | `FORCE_REVERIFY=True` |
# MAGIC
# MAGIC **Batch-only:** one batch ticket + one batch PR per run, and only when at least
# MAGIC `MIN_BATCH_SIZE` (>= 2) vendors qualify. `MIN_BATCH_SIZE=1` raises — use the
# MAGIC Device Labeler Vendor Verifier tab for a one-off single vendor.
# MAGIC
# MAGIC Full runbook: `notebooks/databricks/VENDOR_VERIFIER_EXECUTION_PLAN.md`
# MAGIC
# MAGIC > **Do NOT use the Databricks "Resume" button.** It only replays cells but all Python variables
# MAGIC > (`RUN_ID`, `candidates`, helper functions) are gone after a session reset, so nothing works.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Stage 0 — Init: create tables, define helpers

# COMMAND ----------

RUN_ID = str(uuid.uuid4())[:8]
SUBMITTED_BY = spark.sql("SELECT current_user()").collect()[0][0]

local_tz = timezone("Asia/Jerusalem")
now_local = datetime.datetime.now(local_tz)
WINDOW_END   = now_local
WINDOW_START = now_local - datetime.timedelta(days=DAYS_AGO)

print(f"Run ID:  {RUN_ID}")
print(f"User:    {SUBMITTED_BY}")
print(f"Window:  {WINDOW_START.strftime('%Y-%m-%dT%H:%M:%SZ')} → {WINDOW_END.strftime('%Y-%m-%dT%H:%M:%SZ')}")

# ── Main candidates table (one row per unique vendor string) ──────────────────
spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {CANDIDATES_TABLE} (
        -- Identity
        vendor_name_raw     STRING    NOT NULL,   -- exactly as extracted from the log

        -- Coralogix signal
        orgs_count          INT,                  -- distinct orgs that logged this vendor
        events              LONG,                 -- total log occurrences (lower bound — LimitedWarner)
        orgs                STRING,               -- JSON array of up to 10 org names
        message_examples    STRING,               -- JSON array of up to 2 raw log lines

        -- Pipeline tracking
        status              STRING    NOT NULL,   -- PROCESSING | COMPLETED | FAILED | DUPLICATE
        failure_counter     INT       NOT NULL,   -- how many times Gemini failed for this vendor
        pipeline_run_id     STRING,               -- run_id that last touched this row

        -- Gemini verification output
        verdict             STRING,               -- LEGIT | NOT-MANUFACTURER | BRAND-OF | AMBIGUOUS | GENERIC | SOFTWARE-ONLY | SUSPICIOUS
        official_name       STRING,               -- AI-corrected canonical name
        enum_name           STRING,               -- PascalCase enum value for device.py
        confidence_score    STRING,               -- HIGH | MEDIUM | LOW
        website             STRING,
        hardware_evidence   STRING,
        networking_proof    STRING,
        supported_protocols STRING,               -- JSON array
        mac_oui_check       STRING,               -- Yes | No | Unknown
        device_types        STRING,               -- JSON array
        industries          STRING,               -- JSON array
        analyst_note        STRING,
        technical_artifacts STRING,               -- JSON array of URLs
        search_grounded     BOOLEAN,
        raw_ai_output       STRING,               -- full Gemini response (truncated to 8 KB)
        -- P1 taxonomy fields
        distinct_companies_found INT,             -- how many distinct companies Gemini found
        alternative_companies STRING,             -- JSON list of {{name, website?}} for AMBIGUOUS
        is_original_manufacturer BOOLEAN,         -- False for distributors / integrators / assemblers
        parent_company      STRING,               -- set when BRAND-OF

        -- Alias routing (DUPLICATE gates + LEGIT name normalization)
        duplicate_of        STRING,               -- canonical vendor: existing match OR new official_name
        duplicate_score     DOUBLE,               -- similarity score (0–1); 0 for normalized-name alias
        duplicate_gate      STRING,               -- gate1 | gate2 | normalized
        should_add_alias    BOOLEAN,              -- True = add vendor_name_raw as alias for duplicate_of

        -- Ticket / PR
        jira_ticket         STRING,               -- e.g. NET-1234 (set after ticket is created)
        pr_url              STRING,               -- GitHub batch PR URL
        pr_batch_id         STRING,               -- pipeline run_id that created the PR
        channel             STRING,               -- ctd | integration | lansweeper | ...

        -- Timestamps
        first_seen          TIMESTAMP NOT NULL,
        last_seen           TIMESTAMP NOT NULL,
        verified_at         TIMESTAMP
    )
    USING DELTA
    TBLPROPERTIES (
        delta.enableChangeDataFeed = true,
        description = 'Unknown CTD vendors from Coralogix logs, with AI verification results'
    )
""")

# Migrate existing tables (idempotent — Delta does not support IF NOT EXISTS on ADD COLUMN)
_existing_cols = {f.name for f in spark.table(CANDIDATES_TABLE).schema.fields}
for _col, _type in (
    ("channel", "STRING"),
    ("jira_ticket", "STRING"),
    ("pr_url", "STRING"),
    ("pr_batch_id", "STRING"),
    ("distinct_companies_found", "INT"),
    ("alternative_companies", "STRING"),
    ("is_original_manufacturer", "BOOLEAN"),
    ("parent_company", "STRING"),
):
    if _col not in _existing_cols:
        spark.sql(f"ALTER TABLE {CANDIDATES_TABLE} ADD COLUMN {_col} {_type}")
        print(f"  Added column {_col}")

# ── Pipeline run log (one row per stage per run) ──────────────────────────────
spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {PIPELINE_RUNS_TABLE} (
        run_id          STRING    NOT NULL,
        stage           STRING    NOT NULL,
        status          STRING    NOT NULL,   -- RUNNING | SUCCESS | FAILED
        message         STRING,
        rows_in         LONG,
        rows_out        LONG,
        error_message   STRING,
        error_traceback STRING,
        started_at      TIMESTAMP NOT NULL,
        completed_at    TIMESTAMP             -- NULL while RUNNING
    )
    USING DELTA
    TBLPROPERTIES (delta.enableChangeDataFeed = true)
""")

print("Tables ready.")

# ── SQL escaping ──────────────────────────────────────────────────────────────
def _esc(value) -> str:
    """Escape a value for embedding in a Spark SQL string literal."""
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'")

# ── Pipeline run logging ──────────────────────────────────────────────────────
def _log(stage, status, *, message="", rows_in=0, rows_out=0, error="", tb="", completed=True):
    completed_at_expr = "current_timestamp()" if completed else "NULL"
    spark.sql(f"""
        INSERT INTO {PIPELINE_RUNS_TABLE} VALUES (
            '{_esc(RUN_ID)}', '{_esc(stage)}', '{_esc(status)}',
            '{_esc(message)}',
            {rows_in or 0}, {rows_out or 0},
            '{_esc(error[:2000])}',
            '{_esc(tb[:4000])}',
            current_timestamp(), {completed_at_expr}
        )
    """)

def log_start(stage, rows_in=0):
    _log(stage, "RUNNING", rows_in=rows_in, completed=False)
    print(f"\n{'='*60}")
    print(f"  STAGE: {stage}  (run={RUN_ID})")
    print(f"{'='*60}")

def log_ok(stage, rows_out=0, message=""):
    _log(stage, "SUCCESS", rows_out=rows_out, message=message)
    print(f"  ✅ {stage} SUCCESS — {rows_out} rows out. {message}")

def log_fail(stage, exc, tb_str=""):
    _log(stage, "FAILED", error=str(exc), tb=tb_str)
    print(f"  ❌ {stage} FAILED: {exc}")
    raise RuntimeError(f"Pipeline stopped at [{stage}]: {exc}") from exc

# ── Sentinel variables (prevent NameError cascade if a stage fails) ───────────
_EMPTY_COLS = ["vendor_name_raw", "events", "orgs_count", "orgs", "message_examples"]
cx_df      = pd.DataFrame(columns=_EMPTY_COLS)
candidates = pd.DataFrame(columns=_EMPTY_COLS)
results_summary: list = []

print("Init complete.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Stage 1 — Extract from Coralogix

# COMMAND ----------

STAGE = "EXTRACT"
log_start(STAGE)

CHANNEL_QUERIES: list[tuple[str, str]] = [
    ("ctd", r"""
source logs
| filter $l.subsystemname == 'medetector'
| filter $m.severity == WARNING
| filter $d.message ~ 'Received unknown CTD vendor'
| extract $d.message into vendor
    using regexp(e=/Received unknown CTD vendor: (?<vendor_name>[^(\n]+)/)
| create vendor_name from vendor.vendor_name.trim()
| groupby vendor_name
    agg count() as events,
        approx_count_distinct($d.medigate_org) as orgs_count,
        collect($d.medigate_org, true, 10) as orgs,
        collect($d.message, true, 2) as message_examples
| orderby orgs_count desc, events desc
| limit {limit}
""".strip()),
    ("integration", r"""
source logs
| filter $m.severity == WARNING
| filter $d.message ~ 'Received unknown integration vendor'
| extract $d.message into vendor
    using regexp(e=/Received unknown integration vendor: (?<vendor_name>[^(\n]+) from/)
| create vendor_name from vendor.vendor_name.trim()
| groupby vendor_name
    agg count() as events,
        approx_count_distinct($d.medigate_org) as orgs_count,
        collect($d.medigate_org, true, 10) as orgs,
        collect($d.message, true, 2) as message_examples
| orderby orgs_count desc, events desc
| limit {limit}
""".strip()),
    ("lansweeper", r"""
source logs
| filter $m.severity == WARNING
| filter $d.message ~ 'Could not parse vendor'
| extract $d.message into vendor
    using regexp(e=/Could not parse vendor (.+?) to a known vendor/)
| create vendor_name from vendor.vendor_name.trim()
| groupby vendor_name
    agg count() as events,
        approx_count_distinct($d.medigate_org) as orgs_count,
        collect($d.medigate_org, true, 10) as orgs,
        collect($d.message, true, 2) as message_examples
| orderby orgs_count desc, events desc
| limit {limit}
""".strip()),
]


def _parse_coralogix_ndjson(response_text: str) -> list[dict]:
    raw_rows: list[dict] = []
    for line in response_text.strip().splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if "error" in obj:
            raise RuntimeError(f"Coralogix API error: {obj['error']}")
        for item in obj.get("result", {}).get("results", []):
            ud = item.get("userData") or item.get("user_data") or "{}"
            if isinstance(ud, str):
                ud = json.loads(ud)
            raw_rows.append(ud)
    return raw_rows


def _run_channel_extract(cx_key: str, channel: str, query_template: str) -> pd.DataFrame:
    query = query_template.format(limit=EXTRACT_LIMIT_PER_CHANNEL)
    payload = {
        "query": query,
        "metadata": {
            "startDate": WINDOW_START.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "endDate": WINDOW_END.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "limit": str(EXTRACT_LIMIT_PER_CHANNEL),
            "tier": CORALOGIX_TIER,
        },
    }
    headers = {"Authorization": f"Bearer {cx_key}", "Content-Type": "application/json"}
    response = requests.post(CORALOGIX_ENDPOINT, json=payload, headers=headers, timeout=120)
    response.raise_for_status()
    raw_rows = _parse_coralogix_ndjson(response.text)
    if not raw_rows:
        print(f"    [{channel}] 0 vendors")
        return pd.DataFrame(columns=_EMPTY_COLS + ["channel"])
    df = pd.DataFrame(raw_rows)
    if "vendor_name" in df.columns:
        df = df.rename(columns={"vendor_name": "vendor_name_raw"})
    for col in _EMPTY_COLS:
        if col not in df.columns:
            df[col] = None
    df["channel"] = channel
    print(f"    [{channel}] {len(df)} vendors")
    return df


def _merge_channel_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    from vendor_verifier_similarity import (
        sanitize_vendor_name_raw,
        should_skip_control_ghost,
    )

    if not frames:
        return pd.DataFrame(columns=_EMPTY_COLS + ["channel"])
    combined = pd.concat(frames, ignore_index=True)
    combined["orgs_count"] = pd.to_numeric(combined["orgs_count"], errors="coerce").fillna(0).astype(int)
    combined["events"] = pd.to_numeric(combined["events"], errors="coerce").fillna(0).astype(int)
    combined["vendor_name_raw"] = combined["vendor_name_raw"].astype(str).str.strip()

    # P4 — strip control / zero-width before group-by so byte-variants collapse;
    # skip control-byte ghosts (empty sanitize or corrupt short remnant).
    combined["_sanitized"] = combined["vendor_name_raw"].map(sanitize_vendor_name_raw)
    ghost_mask = combined.apply(
        lambda r: should_skip_control_ghost(r["vendor_name_raw"], r["_sanitized"]),
        axis=1,
    )
    skipped = int(ghost_mask.sum())
    if skipped:
        print(f"  EXTRACT sanitize: skipped {skipped} control-byte ghost candidate(s)")
    combined = combined.loc[~ghost_mask].copy()
    combined["vendor_name_raw"] = combined["_sanitized"]
    combined = combined.drop(columns=["_sanitized"])

    def _merge_channels(series: pd.Series) -> str:
        channels = sorted({str(v) for v in series if v})
        return channels[0] if len(channels) == 1 else json.dumps(channels)

    if combined.empty:
        return pd.DataFrame(columns=_EMPTY_COLS + ["channel"])

    grouped = (
        combined.groupby("vendor_name_raw", as_index=False)
        .agg(
            orgs_count=("orgs_count", "max"),
            events=("events", "sum"),
            orgs=("orgs", "first"),
            message_examples=("message_examples", "first"),
            channel=("channel", _merge_channels),
        )
        .sort_values(["orgs_count", "events"], ascending=False)
        .reset_index(drop=True)
    )
    return grouped


try:
    cx_key = dbutils.secrets.get(scope=CORALOGIX_SECRET_SCOPE, key=CORALOGIX_SECRET_KEY)
    channel_frames = [_run_channel_extract(cx_key, ch, q) for ch, q in CHANNEL_QUERIES]
    cx_df = _merge_channel_frames(channel_frames)
    cx_df = cx_df[
        cx_df["vendor_name_raw"].notna()
        & (cx_df["vendor_name_raw"] != "")
        & (cx_df["vendor_name_raw"].str.lower() != "nan")
        & (~cx_df["vendor_name_raw"].str.isdigit())
    ].reset_index(drop=True)

    print(f"  Coralogix unified extract: {len(cx_df)} distinct vendor strings")
    if not cx_df.empty:
        display(cx_df[["vendor_name_raw", "channel", "orgs_count", "events"]])

    log_ok(STAGE, rows_out=len(cx_df))

except Exception as exc:
    log_fail(STAGE, exc, traceback.format_exc())

# COMMAND ----------

# MAGIC %md
# MAGIC ## Stage 2 — Filter: skip known / below-threshold vendors

# COMMAND ----------

STAGE = "FILTER"
log_start(STAGE, rows_in=len(cx_df))

try:
    if cx_df.empty:
        print("  No Coralogix results — nothing to filter.")
        # candidates keeps its sentinel empty DataFrame
    else:
        filtered = cx_df[cx_df["orgs_count"] >= MIN_ORGS_COUNT].copy()
        print(f"  After MIN_ORGS_COUNT={MIN_ORGS_COUNT}: {len(filtered)} vendors")

        # Skip vendors already processed (never re-run FAILED unless FORCE_REVERIFY)
        if SKIP_STATUSES:
            already_done = {
                row.vendor_name_raw.lower()
                for row in spark.sql(f"""
                    SELECT vendor_name_raw FROM {CANDIDATES_TABLE}
                    WHERE status IN ({', '.join(f"'{s}'" for s in SKIP_STATUSES)})
                """).collect()
            }
            filtered = filtered[~filtered["vendor_name_raw"].str.lower().isin(already_done)]
            print(f"  After skipping {SKIP_STATUSES} ({len(already_done)} known): {len(filtered)} vendors")
        else:
            print("  FORCE_REVERIFY=True — not skipping any vendor")

        if MAX_VENDORS_PER_RUN > 0 and len(filtered) > MAX_VENDORS_PER_RUN:
            filtered = filtered.head(MAX_VENDORS_PER_RUN)
            print(f"  Capped at MAX_VENDORS_PER_RUN={MAX_VENDORS_PER_RUN}: {len(filtered)} vendors")

        candidates = filtered.reset_index(drop=True)

    print(f"\n  → {len(candidates)} vendors will be verified this run")
    if not candidates.empty:
        cols = [c for c in ["vendor_name_raw", "orgs_count", "events"] if c in candidates.columns]
        display(candidates[cols])

    log_ok(STAGE, rows_out=len(candidates))

except Exception as exc:
    log_fail(STAGE, exc, traceback.format_exc())

# COMMAND ----------

# MAGIC %md
# MAGIC ## Stage 3 — Verify each candidate (Gemini Pro + duplicate gates)

# COMMAND ----------

STAGE = "VERIFY"
log_start(STAGE, rows_in=len(candidates))

from vendor_verifier_normalization import generate_enum_name, clean_official_name
from vendor_verifier_similarity import (
    find_similar as _find_similar_impl,
    gate2_should_add_alias,
    resolve_parent_for_brand_of,
)


def needs_log_string_alias(vendor_name_raw: str, official_name: str) -> bool:
    """True when the Coralogix log string must be aliased to stop repeat warnings."""
    raw = vendor_name_raw.strip()
    official = official_name.strip()
    if not raw or not official:
        return False
    return raw != official


def find_similar(input_name: str, registry: list,
                 threshold=SIMILARITY_THRESHOLD, top_n=5) -> list:
    """Thin wrapper — logic lives in vendor_verifier_similarity (pipeline-lib)."""
    return _find_similar_impl(
        input_name,
        registry,
        threshold=threshold,
        top_n=top_n,
        duplicate_threshold=DUPLICATE_THRESHOLD,
    )

# ── Gemini prompt + helpers ───────────────────────────────────────────────────
VERIFICATION_PROMPT = """### Role
You are a Technical Asset Discovery Specialist. Classify unknown vendor strings from OT/IoMT asset discovery logs.

**Vendor string from log:** {vendor_name}
**Website hint:** {vendor_url}

Use Google Search. Decide whether this string maps to exactly one original hardware manufacturer, several companies (homonym), a non-manufacturer brand, or a generic/non-company token.

Look for: physical datasheets (dimensions, weight, power specs), network stack evidence, OT/IoMT protocols, IEEE OUI registration, firmware portals, parent/owner relationships, and whether multiple unrelated companies share the same name.

Respond ONLY with valid JSON:
{{
    "verdict": "LEGIT or NOT-MANUFACTURER or BRAND-OF or AMBIGUOUS or GENERIC or SOFTWARE-ONLY or SUSPICIOUS",
    "official_name": "Best single official name if one exists, else empty",
    "website": "Official URL or empty",
    "is_original_manufacturer": true,
    "parent_company": "Parent/owner if BRAND-OF, else null",
    "acquired_by": "Acquirer if known, else null",
    "distinct_companies_found": 1,
    "alternative_companies": [{{"name": "Company A", "website": "https://..."}}],
    "hardware_evidence": "2-3 physical products with specific specs, or n/a",
    "networking_proof": "Network communication evidence or n/a",
    "supported_protocols": ["list"],
    "mac_oui_check": "Yes/No/Unknown",
    "technical_artifacts": ["URLs"],
    "analyst_note": "Why this verdict; list collisions explicitly for AMBIGUOUS",
    "device_types": ["types"],
    "industries": ["Healthcare", "Industrial", "Enterprise"],
    "confidence_score": "HIGH/MEDIUM/LOW"
}}

Rules:
- If the string matches ≥2 distinct real companies → verdict AMBIGUOUS, distinct_companies_found ≥ 2, fill alternative_companies. Do NOT pick one arbitrarily.
- If distributor / reseller / integrator / retailer / assembler / system builder (not original manufacturer) → NOT-MANUFACTURER and is_original_manufacturer=false.
- If brand/subsidiary/white-label of another company → BRAND-OF and set parent_company (use the owning/parent company name).
- If the brand is widely rebranded / multi-affiliated Chinese industrial PC OEM (e.g. also sold as Iwill / Xin Secco / Yanqin / Ennoconn group brands) and you cannot name one unambiguous first-party manufacturer → AMBIGUOUS or BRAND-OF, never LEGIT.
- If product category, acronym, model number, OCR garbage, BIOS OEMID remnant, or control-character remnant → GENERIC (do NOT invent the most plausible company).
- SOFTWARE-ONLY for OS/cloud/firmware/software brands with no network-connected hardware OEM story.
- LEGIT only when original manufacturer of network-connected OT/IoMT/enterprise hardware AND distinct_companies_found == 1 AND is_original_manufacturer=true AND no parent/owner brand relationship.
"""

REQUIRED_FIELDS = {
    "verdict", "official_name", "website", "hardware_evidence",
    "networking_proof", "supported_protocols", "mac_oui_check",
    "technical_artifacts", "analyst_note", "device_types", "industries",
}
VALID_VERDICTS = {
    "LEGIT", "NOT-MANUFACTURER", "BRAND-OF", "AMBIGUOUS", "GENERIC",
    "SOFTWARE-ONLY", "SUSPICIOUS",
}
NON_PR_VERDICTS = {
    "NOT-MANUFACTURER", "BRAND-OF", "AMBIGUOUS", "GENERIC",
    "SOFTWARE-ONLY", "SUSPICIOUS",
}

def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1].rsplit("\n", 1)[0]
    try:
        return json.loads(t)
    except Exception:
        s, e = t.find("{"), t.rfind("}")
        if s != -1 and e != -1:
            try:
                return json.loads(t[s:e+1])
            except Exception:
                pass
    return None

def _validate(data: dict) -> tuple:
    if not isinstance(data, dict):
        return False, ["Not a dict"]
    missing = REQUIRED_FIELDS - set(data.keys())
    errors = [f"Missing: {missing}"] if missing else []
    v = data.get("verdict")
    if v and v not in VALID_VERDICTS:
        errors.append(f"Bad verdict: {v!r}")
    return len(errors) == 0, errors


def is_new_vendor_auto_pr_eligible(result: dict | None, *, confidence: str | None = None) -> bool:
    """P1 gate: LEGIT + original manufacturer + unique company + confidence band."""
    d = result or {}
    verdict = (d.get("verdict") or "").strip()
    if verdict != "LEGIT":
        return False
    conf = (confidence if confidence is not None else d.get("confidence_score") or "").strip().upper()
    if conf not in AUTO_PR_CONFIDENCES:
        return False
    if d.get("is_original_manufacturer") is False:
        return False
    distinct = d.get("distinct_companies_found")
    if isinstance(distinct, int) and distinct != 1:
        return False
    if isinstance(distinct, str) and distinct.strip().isdigit() and int(distinct.strip()) != 1:
        return False
    return True

def gemini_verify(vendor_name: str, gemini_key: str) -> tuple:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=gemini_key)
    for attempt in range(GEMINI_MAX_RETRIES):
        try:
            prompt = VERIFICATION_PROMPT.format(
                vendor_name=vendor_name,
                vendor_url="Not provided",
            )
            config = types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.0,
                max_output_tokens=4096,
            )
            resp = client.models.generate_content(
                model=GEMINI_MODEL_ID, contents=[prompt], config=config
            )
            raw = resp.text or ""
            data = _extract_json(raw)
            if data is None:
                raise ValueError("JSON parse failed")
            ok, errs = _validate(data)
            if not ok:
                raise ValueError(str(errs))
            return data, raw, True
        except Exception as exc:
            print(f"    Gemini attempt {attempt+1}/{GEMINI_MAX_RETRIES}: {exc}")
            if attempt < GEMINI_MAX_RETRIES - 1:
                time.sleep(2 * (attempt + 1))
    return None, "", False

# ── Upsert into coralogix_vendor_candidates ───────────────────────────────────
def _upsert_candidate(vendor_name_raw: str, cx_row: dict, *,
                      status: str,
                      failure_delta: int = 0,
                      result: dict | None = None,
                      raw_ai_output: str = "",
                      search_grounded: bool = False,
                      official_name: str = "",
                      enum_name: str = "",
                      duplicate_of: str = "",
                      duplicate_score: float = 0.0,
                      duplicate_gate: str = "",
                      should_add_alias: bool = False) -> None:
    d = result or {}
    orgs_json = json.dumps(list(cx_row.get("orgs") or []))
    msg_json  = json.dumps(list(cx_row.get("message_examples") or []))

    verified_at_expr = "current_timestamp()" if status == "COMPLETED" else "NULL"

    distinct_raw = d.get("distinct_companies_found")
    if isinstance(distinct_raw, bool):
        distinct_sql = "NULL"
    elif isinstance(distinct_raw, int):
        distinct_sql = str(int(distinct_raw))
    elif isinstance(distinct_raw, str) and distinct_raw.strip().lstrip("-").isdigit():
        distinct_sql = str(int(distinct_raw.strip()))
    else:
        distinct_sql = "NULL"

    alts = d.get("alternative_companies")
    if isinstance(alts, str):
        alts_json = alts
    else:
        alts_json = json.dumps(alts or [])

    oem = d.get("is_original_manufacturer")
    if isinstance(oem, bool):
        oem_sql = str(oem).lower()
    else:
        oem_sql = "NULL"

    parent = d.get("parent_company")
    if parent in (None, "null", "None"):
        parent_sql = ""
    else:
        parent_sql = str(parent)

    spark.sql(f"""
        MERGE INTO {CANDIDATES_TABLE} AS t
        USING (SELECT
            '{_esc(vendor_name_raw)}'                               AS vendor_name_raw,
            {int(cx_row.get("orgs_count") or 0)}                   AS orgs_count,
            {int(cx_row.get("events") or 0)}                       AS events,
            '{_esc(orgs_json)}'                                     AS orgs,
            '{_esc(msg_json)}'                                      AS message_examples,
            '{_esc(status)}'                                        AS status,
            {failure_delta}                                         AS failure_delta,
            '{_esc(RUN_ID)}'                                        AS pipeline_run_id,
            '{_esc(d.get("verdict",""))}'                           AS verdict,
            '{_esc(official_name)}'                                 AS official_name,
            '{_esc(enum_name)}'                                     AS enum_name,
            '{_esc(d.get("confidence_score",""))}'                  AS confidence_score,
            '{_esc(d.get("website",""))}'                           AS website,
            '{_esc(d.get("hardware_evidence",""))}'                 AS hardware_evidence,
            '{_esc(d.get("networking_proof",""))}'                  AS networking_proof,
            '{_esc(json.dumps(d.get("supported_protocols",[])))}'   AS supported_protocols,
            '{_esc(d.get("mac_oui_check",""))}'                     AS mac_oui_check,
            '{_esc(json.dumps(d.get("device_types",[])))}'          AS device_types,
            '{_esc(json.dumps(d.get("industries",[])))}'            AS industries,
            '{_esc(d.get("analyst_note",""))}'                      AS analyst_note,
            '{_esc(json.dumps(d.get("technical_artifacts",[])))}'   AS technical_artifacts,
            {str(search_grounded).lower()}                          AS search_grounded,
            '{_esc(raw_ai_output[:8000])}'                          AS raw_ai_output,
            '{_esc(duplicate_of)}'                                  AS duplicate_of,
            {float(duplicate_score)}                                AS duplicate_score,
            '{_esc(duplicate_gate)}'                                AS duplicate_gate,
            {str(should_add_alias).lower()}                         AS should_add_alias,
            '{_esc(str(cx_row.get("channel") or ""))}'              AS channel,
            {distinct_sql}                                          AS distinct_companies_found,
            '{_esc(alts_json)}'                                     AS alternative_companies,
            {oem_sql}                                               AS is_original_manufacturer,
            '{_esc(parent_sql)}'                                    AS parent_company
        ) AS s ON t.vendor_name_raw = s.vendor_name_raw
        WHEN MATCHED THEN UPDATE SET
            t.orgs_count        = s.orgs_count,
            t.events            = s.events,
            t.orgs              = s.orgs,
            t.message_examples  = s.message_examples,
            t.status            = s.status,
            t.failure_counter   = COALESCE(t.failure_counter, 0) + s.failure_delta,
            t.pipeline_run_id   = s.pipeline_run_id,
            t.verdict           = NULLIF(s.verdict, ''),
            t.official_name     = NULLIF(s.official_name, ''),
            t.enum_name         = NULLIF(s.enum_name, ''),
            t.confidence_score  = NULLIF(s.confidence_score, ''),
            t.website           = NULLIF(s.website, ''),
            t.hardware_evidence = NULLIF(s.hardware_evidence, ''),
            t.networking_proof  = NULLIF(s.networking_proof, ''),
            t.supported_protocols = NULLIF(s.supported_protocols, '[]'),
            t.mac_oui_check     = NULLIF(s.mac_oui_check, ''),
            t.device_types      = NULLIF(s.device_types, '[]'),
            t.industries        = NULLIF(s.industries, '[]'),
            t.analyst_note      = NULLIF(s.analyst_note, ''),
            t.technical_artifacts = NULLIF(s.technical_artifacts, '[]'),
            t.search_grounded   = s.search_grounded,
            t.raw_ai_output     = NULLIF(s.raw_ai_output, ''),
            t.duplicate_of      = NULLIF(s.duplicate_of, ''),
            t.duplicate_score   = NULLIF(s.duplicate_score, 0.0),
            t.duplicate_gate    = NULLIF(s.duplicate_gate, ''),
            t.should_add_alias  = s.should_add_alias,
            t.channel           = NULLIF(s.channel, ''),
            t.distinct_companies_found = s.distinct_companies_found,
            t.alternative_companies = NULLIF(s.alternative_companies, '[]'),
            t.is_original_manufacturer = s.is_original_manufacturer,
            t.parent_company    = NULLIF(s.parent_company, ''),
            t.last_seen         = current_timestamp(),
            t.verified_at       = {verified_at_expr}
        WHEN NOT MATCHED THEN INSERT (
            vendor_name_raw, orgs_count, events, orgs, message_examples,
            status, failure_counter, pipeline_run_id,
            verdict, official_name, enum_name, confidence_score,
            website, hardware_evidence, networking_proof, supported_protocols,
            mac_oui_check, device_types, industries, analyst_note,
            technical_artifacts, search_grounded, raw_ai_output,
            duplicate_of, duplicate_score, duplicate_gate, should_add_alias, channel,
            distinct_companies_found, alternative_companies,
            is_original_manufacturer, parent_company,
            first_seen, last_seen, verified_at
        ) VALUES (
            s.vendor_name_raw, s.orgs_count, s.events, s.orgs, s.message_examples,
            s.status, s.failure_delta, s.pipeline_run_id,
            NULLIF(s.verdict,''), NULLIF(s.official_name,''), NULLIF(s.enum_name,''),
            NULLIF(s.confidence_score,''), NULLIF(s.website,''),
            NULLIF(s.hardware_evidence,''), NULLIF(s.networking_proof,''),
            NULLIF(s.supported_protocols,'[]'), NULLIF(s.mac_oui_check,''),
            NULLIF(s.device_types,'[]'), NULLIF(s.industries,'[]'),
            NULLIF(s.analyst_note,''), NULLIF(s.technical_artifacts,'[]'),
            s.search_grounded, NULLIF(s.raw_ai_output,''),
            NULLIF(s.duplicate_of,''), NULLIF(s.duplicate_score, 0.0),
            NULLIF(s.duplicate_gate,''), s.should_add_alias, NULLIF(s.channel,''),
            s.distinct_companies_found, NULLIF(s.alternative_companies, '[]'),
            s.is_original_manufacturer, NULLIF(s.parent_company, ''),
            current_timestamp(), current_timestamp(), {verified_at_expr}
        )
    """)

def _set_batch_jira_ticket(vendor_names: list[str], ticket_key: str) -> None:
    """Stamp the run's single batch ticket onto every row it covers."""
    if not vendor_names:
        return
    name_list = ", ".join(f"'{_esc(name)}'" for name in vendor_names)
    spark.sql(f"""
        UPDATE {CANDIDATES_TABLE}
        SET jira_ticket = '{_esc(ticket_key)}', last_seen = current_timestamp()
        WHERE vendor_name_raw IN ({name_list})
    """)

# ── Main verification loop ────────────────────────────────────────────────────
try:
    if candidates.empty:
        print("  No candidates — skipping verification.")
        log_ok(STAGE, rows_out=0, message="No candidates")
    else:
        gemini_key = dbutils.secrets.get(scope=GEMINI_SECRET_SCOPE, key=GEMINI_SECRET_KEY)
        completed = failed = duplicated = 0
        consecutive_failures = 0
        circuit_breaker_triggered = False
        circuit_breaker_skipped = 0
        total_candidates = len(candidates)

        # Load vendor registry once (production silver table + previously verified names)
        print("  Loading vendor registry...")
        prod_df = spark.sql(f"""
            SELECT DISTINCT vendor AS vendor_name
            FROM {SILVER_VENDORS_TABLE}
            WHERE vendor IS NOT NULL AND TRIM(vendor) != ''
        """)
        try:
            ver_df = spark.sql(f"""
                SELECT DISTINCT official_name AS vendor_name
                FROM {CANDIDATES_TABLE}
                WHERE official_name IS NOT NULL AND TRIM(official_name) != ''
            """)
            vendor_registry = [r.vendor_name for r in prod_df.union(ver_df).distinct().collect()]
        except Exception:
            vendor_registry = [r.vendor_name for r in prod_df.collect()]
        print(f"  Registry size: {len(vendor_registry)}")

        for proc_idx, (_, row) in enumerate(candidates.iterrows()):
            if circuit_breaker_triggered:
                break

            vendor_name = str(row["vendor_name_raw"]).strip()
            if not vendor_name or vendor_name.lower() == "nan" or vendor_name.isdigit():
                print(f"  [{proc_idx+1}] Skipping invalid vendor_name: {vendor_name!r}")
                continue

            cx_row = row.to_dict()
            print(f"\n  [{proc_idx+1}/{total_candidates}] {vendor_name!r}  "
                  f"(orgs={row.get('orgs_count', 0)}, events={row.get('events', 0)})")

            # Per-vendor isolation: one bad write / network error must not abort the run
            try:
                # ── Gate 1: raw name duplicate check ────────────────────────
                similar_g1 = find_similar(vendor_name, vendor_registry)
                gate1_dup = bool(similar_g1) and (
                    similar_g1[0][2] == "exact" or similar_g1[0][1] >= DUPLICATE_THRESHOLD
                )
                if gate1_dup:
                    match_name, match_score, match_type = similar_g1[0]
                    print(f"    Gate 1 DUPLICATE ({match_type} {match_score:.0%}): '{match_name}'")
                    print(f"    → should_add_alias: '{vendor_name}' as alias for '{match_name}'")
                    _upsert_candidate(
                        vendor_name, cx_row, status="DUPLICATE",
                        duplicate_of=match_name, duplicate_score=match_score,
                        duplicate_gate="gate1", should_add_alias=True,
                    )
                    results_summary.append({
                        "vendor_name": vendor_name, "outcome": "DUPLICATE",
                        "detail": f"Gate 1 → matches '{match_name}' ({match_score:.0%})",
                        "orgs_count": row.get("orgs_count"),
                    })
                    duplicated += 1
                    consecutive_failures = 0
                    time.sleep(0.5)
                    continue

                if similar_g1:
                    print("    Gate 1 similar (below threshold): "
                          + ", ".join(f"{n} {s:.0%}" for n, s, _ in similar_g1[:2]))

                # ── Mark as in-progress ─────────────────────────────────────
                _upsert_candidate(vendor_name, cx_row, status="PROCESSING")

                # ── Gemini verification ──────────────────────────────────────
                result_data, raw_response, search_grounded = gemini_verify(vendor_name, gemini_key)

                if result_data is None:
                    print(f"    Gemini FAILED after all retries.")
                    _upsert_candidate(vendor_name, cx_row, status="FAILED", failure_delta=1)
                    results_summary.append({
                        "vendor_name": vendor_name, "outcome": "FAILED",
                        "detail": "Gemini failed after all retries",
                        "orgs_count": row.get("orgs_count"),
                    })
                    failed += 1
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_GEMINI_FAILURES:
                        circuit_breaker_skipped = total_candidates - proc_idx - 1
                        print(
                            f"\n  ⛔ Circuit breaker: {consecutive_failures} consecutive Gemini "
                            f"failures — stopping VERIFY ({circuit_breaker_skipped} vendor(s) left untouched)."
                        )
                        circuit_breaker_triggered = True
                    time.sleep(1)
                    continue

                # ── Gate 2: normalized name duplicate check ──────────────────
                official_name = clean_official_name(result_data.get("official_name", vendor_name))
                if official_name.lower() != vendor_name.lower():
                    similar_g2 = find_similar(official_name, vendor_registry)
                    gate2_dup = bool(similar_g2) and (
                        similar_g2[0][2] == "exact" or similar_g2[0][1] >= DUPLICATE_THRESHOLD
                    )
                    if gate2_dup:
                        match_name, match_score, match_type = similar_g2[0]
                        should_add_alias = gate2_should_add_alias(result_data.get("verdict"))
                        print(f"    Gate 2 DUPLICATE: '{official_name}' → '{match_name}' ({match_score:.0%})")
                        print(
                            f"    → should_add_alias={should_add_alias}: '{vendor_name}' "
                            f"as alias for '{match_name}' (verdict={result_data.get('verdict')})"
                        )
                        _upsert_candidate(
                            vendor_name, cx_row, status="DUPLICATE",
                            result=result_data, raw_ai_output=raw_response,
                            search_grounded=search_grounded,
                            official_name=official_name,
                            duplicate_of=match_name, duplicate_score=match_score,
                            duplicate_gate="gate2", should_add_alias=should_add_alias,
                        )
                        results_summary.append({
                            "vendor_name": vendor_name, "outcome": "DUPLICATE",
                            "detail": f"Gate 2 → '{official_name}' matches '{match_name}'",
                            "orgs_count": row.get("orgs_count"),
                        })
                        duplicated += 1
                        consecutive_failures = 0
                        time.sleep(0.5)
                        continue

                # ── P2: BRAND-OF / parent / acquirer → alias if parent in registry ──
                parent_hit = resolve_parent_for_brand_of(
                    verdict=result_data.get("verdict"),
                    official_name=official_name,
                    parent_company=result_data.get("parent_company"),
                    acquired_by=result_data.get("acquired_by"),
                    registry=vendor_registry,
                    duplicate_threshold=DUPLICATE_THRESHOLD,
                )
                if parent_hit:
                    match_name, match_score, match_type = parent_hit
                    print(
                        f"    P2 parent resolve ({match_type} {match_score:.0%}): "
                        f"{official_name!r} → {match_name!r}"
                    )
                    print(
                        f"    → should_add_alias: '{vendor_name}' as alias for '{match_name}'"
                    )
                    _upsert_candidate(
                        vendor_name, cx_row, status="DUPLICATE",
                        result=result_data, raw_ai_output=raw_response,
                        search_grounded=search_grounded,
                        official_name=official_name,
                        duplicate_of=match_name, duplicate_score=match_score,
                        duplicate_gate="p2-parent", should_add_alias=True,
                    )
                    results_summary.append({
                        "vendor_name": vendor_name, "outcome": "DUPLICATE",
                        "detail": f"P2 → '{official_name}' parent '{match_name}' ({match_type})",
                        "orgs_count": row.get("orgs_count"),
                    })
                    duplicated += 1
                    consecutive_failures = 0
                    time.sleep(0.5)
                    continue

                enum_name = generate_enum_name(official_name)

                verdict = result_data.get("verdict", "SUSPICIOUS")
                add_log_alias = (
                    verdict == "LEGIT"
                    and needs_log_string_alias(vendor_name, official_name)
                )
                if add_log_alias:
                    print(
                        f"    → should_add_alias: {vendor_name!r} "
                        f"(log string) → {official_name!r} (new vendor)"
                    )
                print(f"    Verdict: {verdict}  Official: {official_name!r}  "
                      f"Confidence: {result_data.get('confidence_score', '?')}")

                # ── Write completed result ───────────────────────────────────
                _upsert_candidate(
                    vendor_name, cx_row, status="COMPLETED",
                    result=result_data, raw_ai_output=raw_response,
                    search_grounded=search_grounded,
                    official_name=official_name, enum_name=enum_name,
                    duplicate_of=official_name if add_log_alias else "",
                    duplicate_gate="normalized" if add_log_alias else "",
                    should_add_alias=add_log_alias,
                )
                results_summary.append({
                    "vendor_name":   vendor_name,
                    "outcome":       verdict,
                    "official_name": official_name,
                    "enum_name":     enum_name,
                    "confidence":    result_data.get("confidence_score", ""),
                    "orgs_count":    row.get("orgs_count"),
                    "detail":        result_data.get("analyst_note", ""),
                })
                completed += 1
                consecutive_failures = 0
                time.sleep(2)   # Rate-limit Gemini calls

            except Exception as vendor_exc:
                print(f"    ⚠️  Unexpected error for '{vendor_name}': {vendor_exc}")
                print(f"    Marking FAILED and continuing.")
                try:
                    _upsert_candidate(vendor_name, cx_row, status="FAILED", failure_delta=1)
                except Exception:
                    print(f"    (Delta write also failed — skipping status update)")
                results_summary.append({
                    "vendor_name": vendor_name, "outcome": "FAILED",
                    "detail": str(vendor_exc)[:200], "orgs_count": row.get("orgs_count"),
                })
                failed += 1
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_GEMINI_FAILURES:
                    circuit_breaker_skipped = total_candidates - proc_idx - 1
                    print(
                        f"\n  ⛔ Circuit breaker: {consecutive_failures} consecutive verify "
                        f"failures — stopping VERIFY ({circuit_breaker_skipped} vendor(s) left untouched)."
                    )
                    circuit_breaker_triggered = True
                time.sleep(1)

        print(
            f"\n  Run complete — completed={completed} duplicated={duplicated} failed={failed}"
            + (f" circuit_breaker_skipped={circuit_breaker_skipped}" if circuit_breaker_triggered else "")
        )
        verify_msg = f"completed={completed} duplicated={duplicated} failed={failed}"
        if circuit_breaker_triggered:
            verify_msg += f" circuit_breaker_skipped={circuit_breaker_skipped}"
        log_ok(STAGE, rows_out=completed + duplicated + failed, message=verify_msg)

except Exception as exc:
    log_fail(STAGE, exc, traceback.format_exc())

# COMMAND ----------

# MAGIC %md
# MAGIC ## Stage 4 — Report

# COMMAND ----------

STAGE = "REPORT"
log_start(STAGE)

try:
    summary_df = pd.DataFrame(results_summary) if results_summary else pd.DataFrame(
        columns=["vendor_name", "outcome", "official_name", "enum_name",
                 "confidence", "orgs_count", "detail"]
    )
    print(f"\n  Run {RUN_ID} summary ({len(summary_df)} vendors processed):")
    display(summary_df)
    log_ok(STAGE, rows_out=len(summary_df))

except Exception as exc:
    log_fail(STAGE, exc, traceback.format_exc())

# COMMAND ----------

# MAGIC %md
# MAGIC ## Stage 5 — Create the batch Jira ticket
# MAGIC
# MAGIC Creates **exactly one** NET ticket per run, covering every qualifying row:
# MAGIC - **LEGIT** vendors — listed under "New vendors" with enum, orgs, confidence, website
# MAGIC - **LEGIT + normalized name** — the same bullet also requests `{vendor_name_raw}` as a
# MAGIC   `VENDOR_ALIASES` entry so the exact Coralogix log string stops reappearing
# MAGIC - **Duplicate alias candidates** — listed under "Aliases" as `raw → duplicate_of`
# MAGIC
# MAGIC Every row in the batch gets the same `jira_ticket`, which the batch PR reuses as its
# MAGIC lead ticket. There is no per-vendor ticket path.
# MAGIC
# MAGIC **Safety:** requires `CREATE_TICKETS=True`. Default scope is `current_run`
# MAGIC (`pipeline_run_id = RUN_ID`) so resume/VERIFY runs cannot flood Jira with the
# MAGIC historical backlog. Use `TICKETS_SCOPE=all_pending` only when intentionally
# MAGIC clearing the backlog. Skips rows that already have `jira_ticket`. Fewer than
# MAGIC `MIN_BATCH_SIZE` qualifying rows → no ticket at all (batch-only policy).
# MAGIC Uses credentials from the `vendor-validation-app` Databricks secret scope.

# COMMAND ----------

import base64
import re as _re

JIRA_BASE_URL        = "https://team82.atlassian.net"
JIRA_PROJECT_KEY     = "NET"
JIRA_DEFAULT_ASSIGNEE = "712020:af3f3d43-c255-456b-8230-4d2bcec470ee"  # Ido Moisi
JIRA_TEAM_FIELD       = "customfield_11259"
JIRA_TEAM_CLASSIFICATION_ID = "16991"  # Classification (Data) — required on NET tasks

def _jira_creds() -> tuple[str, str]:
    email = dbutils.secrets.get(scope="vendor-validation-app", key="jira_email")
    token = dbutils.secrets.get(scope="vendor-validation-app", key="jira_api_token")
    return email, token

def _jira_headers(email: str, token: str) -> dict:
    encoded = base64.b64encode(f"{email}:{token}".encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

def _adf(lines: list[str]) -> dict:
    """Build a minimal Jira ADF doc from a list of markdown-ish lines."""
    content = []
    for line in lines:
        if line == "---":
            content.append({"type": "rule"})
            continue
        if line == "":
            content.append({"type": "paragraph", "content": [{"type": "text", "text": " "}]})
            continue
        parts = _re.split(r"\*\*(.+?)\*\*", line)
        para = []
        for i, p in enumerate(parts):
            if not p:
                continue
            if i % 2 == 1:
                para.append({"type": "text", "text": p, "marks": [{"type": "strong"}]})
            else:
                para.append({"type": "text", "text": p})
        content.append({"type": "paragraph", "content": para})
    return {"type": "doc", "version": 1, "content": content}

def _create_ticket(summary: str, description_lines: list[str], email: str, token: str) -> tuple[str, str]:
    payload = {
        "fields": {
            "project":     {"key": JIRA_PROJECT_KEY},
            "summary":     summary,
            "issuetype":   {"name": "Task"},
            "description": _adf(description_lines),
            "labels":      ["maestro"],
            "assignee":    {"id": JIRA_DEFAULT_ASSIGNEE},
            JIRA_TEAM_FIELD: {"id": JIRA_TEAM_CLASSIFICATION_ID},
        }
    }
    resp = requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/issue",
        json=payload,
        headers=_jira_headers(email, token),
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Jira {resp.status_code}: {resp.text[:300]}")
    key = resp.json()["key"]
    return key, f"{JIRA_BASE_URL}/browse/{key}"

def _new_vendor_line(row) -> str:
    """One bullet per new vendor — full evidence stays in Delta / the dashboard."""
    name     = row.vendor_name_raw
    official = row.official_name or name
    enum     = row.enum_name or ""
    website  = row.website or "N/A"
    confidence = row.confidence_score or "?"
    orgs     = row.orgs_count or 0

    line = f"- **{official}** — {enum} = \"{official}\""
    if name.strip() != official.strip():
        line += f" · log \"{name}\""
    line += f" · {orgs} org(s) · confidence {confidence} · {website}"
    if row.should_add_alias and name.strip() != official.strip():
        # Without the raw log string as an alias, production keeps firing the warning.
        line += f" · also alias \"{name}\""
    return line

def _alias_line(row) -> str:
    """One bullet per alias-only lead."""
    raw   = row.vendor_name_raw
    canon = row.duplicate_of or row.official_name or "?"
    score = round((row.duplicate_score or 0) * 100, 1)
    gate  = row.duplicate_gate or "?"
    orgs  = row.orgs_count or 0
    return f"- \"{raw}\" → **{canon}** — {score}% ({gate}) · {orgs} org(s)"

# Jira caps a description field at ~32k chars; an all_pending backlog batch would blow
# past that and fail ticket creation for the whole run. Truncate the listing instead —
# the batch PR body and Delta always carry the full set.
TICKET_MAX_LISTED = 60
TICKET_ALIAS_RESERVE = 15   # keep room for aliases when new vendors would eat the budget

def _batch_ticket(rows, email: str, token: str, *, run_id: str, scope: str) -> tuple[str, str]:
    """Create the single NET ticket that covers this run's whole batch.

    The pipeline is batch-only: one ticket per run, shared by every row in the batch and
    reused as the lead ticket for the batch PR.
    """
    new_rows   = [r for r in rows if r.status == "COMPLETED"]
    alias_rows = [r for r in rows if r.status != "COMPLETED"]

    alias_reserve = min(len(alias_rows), TICKET_ALIAS_RESERVE)
    new_shown     = new_rows[: max(0, TICKET_MAX_LISTED - alias_reserve)]
    alias_shown   = alias_rows[: TICKET_MAX_LISTED - len(new_shown)]

    def _more(shown: list, total: list) -> list[str]:
        hidden = len(total) - len(shown)
        if hidden <= 0:
            return []
        return [
            f"- … and **{hidden} more** — full list in the batch PR and in "
            f"`coralogix_vendor_candidates` for run `{run_id}`"
        ]

    lines = [
        f"Batch of **{len(rows)} vendor lead(s)** from the Coralogix Vendor Verifier pipeline.",
        f"**{len(new_rows)}** new vendor(s), **{len(alias_rows)}** alias(es).",
        "",
        f"**Run ID:** `{run_id}`  **Scope:** {scope}",
        "",
        "One batch PR to medigator `staging` covers every change below.",
    ]

    if new_rows:
        lines += ["", "---", f"**New vendors ({len(new_rows)})**", ""]
        lines += [_new_vendor_line(row) for row in new_shown]
        lines += _more(new_shown, new_rows)

    if alias_rows:
        lines += ["", "---", f"**Aliases ({len(alias_rows)})**", ""]
        lines += [_alias_line(row) for row in alias_shown]
        lines += _more(alias_shown, alias_rows)

    lines += [
        "",
        "---",
        "**Files to modify:**",
        "",
        "- `medigator/common/domain_model/xiot/device.py` — new `Vendor` enum members above "
        "`# Vendors from manuf file #`, plus the `VENDOR_ALIASES` entries listed above",
        "- `medigator/common/domain_model/intels/vulnerabilities/types.py` — matching "
        "`VulnerabilityRelevanceSource` members and `manufacturer_sources` entries "
        "(new vendors only; aliases need no change)",
    ]

    summary = (
        f"[Vendor Verifier] Batch: {len(new_rows)} new vendor(s) + "
        f"{len(alias_rows)} alias(es)"
    )
    return _create_ticket(summary, lines, email, token)

# ── Run ticket creation ───────────────────────────────────────────────────────
STAGE = "CREATE_TICKETS"
_VALID_TICKETS_SCOPES = ("current_run", "all_pending")

if not CREATE_TICKETS:
    print("CREATE_TICKETS=False — skipping Jira ticket stage.")
    _log(STAGE, "SUCCESS", message="skipped (CREATE_TICKETS=False)", rows_out=0)
elif TICKETS_SCOPE not in _VALID_TICKETS_SCOPES:
    raise ValueError(
        f"Invalid TICKETS_SCOPE={TICKETS_SCOPE!r}; expected one of {_VALID_TICKETS_SCOPES}"
    )
else:
    log_start(STAGE)
    try:
        email, token = _jira_creds()

        scope_filter = ""
        if TICKETS_SCOPE == "current_run":
            scope_filter = f"AND pipeline_run_id = '{_esc(RUN_ID)}'"
            print(f"  TICKETS_SCOPE=current_run — only rows with pipeline_run_id={RUN_ID}")
        else:
            print(
                "  ⚠️  TICKETS_SCOPE=all_pending — will ticket the full backlog "
                "(every P1-eligible LEGIT / alias row with no jira_ticket)."
            )

        conf_list = ", ".join(f"'{c}'" for c in sorted(AUTO_PR_CONFIDENCES))
        pending = spark.sql(f"""
            SELECT
                vendor_name_raw, status, verdict, official_name, enum_name,
                confidence_score, website, hardware_evidence, networking_proof,
                supported_protocols, device_types, industries, analyst_note,
                duplicate_of, duplicate_score, duplicate_gate, orgs_count, should_add_alias,
                pipeline_run_id, distinct_companies_found, is_original_manufacturer,
                parent_company, alternative_companies
            FROM {CANDIDATES_TABLE}
            WHERE jira_ticket IS NULL
              AND (
                   (
                        status = 'COMPLETED'
                    AND verdict = 'LEGIT'
                    AND confidence_score IN ({conf_list})
                    AND COALESCE(distinct_companies_found, 1) = 1
                    AND (is_original_manufacturer IS NULL OR is_original_manufacturer = true)
                   )
                OR (status = 'DUPLICATE' AND should_add_alias = true)
              )
              {scope_filter}
            ORDER BY orgs_count DESC NULLS LAST
        """).collect()

        # Preview before any Jira writes (operator can cancel the run if count looks wrong)
        print(f"  {len(pending)} vendor(s) qualify for the batch under scope={TICKETS_SCOPE!r}")
        for row in pending[:20]:
            kind = "new vendor" if row.status == "COMPLETED" else "alias"
            print(
                f"    preview [{kind}] {row.vendor_name_raw!r} "
                f"orgs={row.orgs_count} run={row.pipeline_run_id}"
            )
        if len(pending) > 20:
            print(f"    ... and {len(pending) - 20} more")

        if len(pending) < MIN_BATCH_SIZE:
            # Batch-only: never open a ticket for a single vendor. Rows keep
            # jira_ticket = NULL and roll into the next run's batch.
            print(
                f"  Held back — {len(pending)} qualifying vendor(s) < MIN_BATCH_SIZE="
                f"{MIN_BATCH_SIZE}. No ticket created; rows stay pending for the next run."
            )
            log_ok(
                STAGE,
                rows_out=0,
                message=(
                    f"held back: {len(pending)} qualifying row(s) < "
                    f"MIN_BATCH_SIZE={MIN_BATCH_SIZE} (scope={TICKETS_SCOPE})"
                ),
            )
        else:
            key, url = _batch_ticket(
                pending, email, token, run_id=RUN_ID, scope=TICKETS_SCOPE
            )
            _set_batch_jira_ticket([r.vendor_name_raw for r in pending], key)
            print(f"  ✅ {key}  batch of {len(pending)} vendor(s)  → {url}")
            log_ok(
                STAGE,
                rows_out=len(pending),
                message=(
                    f"batch ticket {key} covers {len(pending)} vendor(s) "
                    f"(scope={TICKETS_SCOPE})"
                ),
            )

    except Exception as exc:
        log_fail(STAGE, exc, traceback.format_exc())

# COMMAND ----------

# MAGIC %md
# MAGIC ## Stage 6 — Create batch GitHub PR (optional)
# MAGIC
# MAGIC Set `CREATE_PR = True` after reviewing Delta for mega backfill.
# MAGIC Weekly `RUN_MODE` sets this automatically. Uses `vendor_verifier_batch_pr.py`.
# MAGIC
# MAGIC Opens **one** PR for every row that has a batch ticket and no `pr_url` yet. Fewer than
# MAGIC `MIN_BATCH_SIZE` qualifying rows → no PR; they roll into the next run's batch.

# COMMAND ----------

STAGE = "CREATE_PR"
if not CREATE_PR:
    print("CREATE_PR=False — skipping batch PR stage.")
    _log(STAGE, "SUCCESS", message="skipped (CREATE_PR=False)", rows_out=0)
else:
    log_start(STAGE)
    try:
        from vendor_verifier_batch_pr import create_batch_pr_from_spark

        def _dbutils_secret(scope: str, key: str) -> str:
            return dbutils.secrets.get(scope=scope, key=key)

        pr_url = create_batch_pr_from_spark(
            spark,
            CANDIDATES_TABLE,
            RUN_ID,
            PR_MODE,
            auto_pr_confidences=AUTO_PR_CONFIDENCES,
            get_secret=_dbutils_secret,
            min_batch_size=MIN_BATCH_SIZE,
        )
        if pr_url:
            log_ok(STAGE, rows_out=1, message=pr_url)
        else:
            log_ok(
                STAGE,
                rows_out=0,
                message=(
                    "no batch opened — fewer than "
                    f"MIN_BATCH_SIZE={MIN_BATCH_SIZE} rows qualified"
                ),
            )
    except Exception as exc:
        log_fail(STAGE, exc, traceback.format_exc())

# COMMAND ----------

# MAGIC %sql
# MAGIC -- Pipeline run log — stages, durations, errors
# MAGIC SELECT
# MAGIC   run_id, stage, status, rows_in, rows_out, message, error_message,
# MAGIC   started_at, completed_at,
# MAGIC   ROUND(unix_timestamp(completed_at) - unix_timestamp(started_at), 1) AS duration_s
# MAGIC FROM `s3-write-bucket`.`ido`.coralogix_pipeline_runs
# MAGIC ORDER BY started_at DESC
# MAGIC LIMIT 100

# COMMAND ----------

# MAGIC %sql
# MAGIC -- All candidates — most impactful first
# MAGIC SELECT
# MAGIC   vendor_name_raw,
# MAGIC   status,
# MAGIC   orgs_count,
# MAGIC   events,
# MAGIC   verdict,
# MAGIC   official_name,
# MAGIC   enum_name,
# MAGIC   confidence_score,
# MAGIC   duplicate_of,
# MAGIC   should_add_alias,
# MAGIC   jira_ticket,
# MAGIC   failure_counter,
# MAGIC   pipeline_run_id,
# MAGIC   verified_at
# MAGIC FROM `s3-write-bucket`.`ido`.coralogix_vendor_candidates
# MAGIC ORDER BY orgs_count DESC NULLS LAST, events DESC NULLS LAST

# COMMAND ----------

# MAGIC %sql
# MAGIC -- Alias candidates with ticket status (duplicate match OR normalized LEGIT name)
# MAGIC SELECT
# MAGIC   vendor_name_raw                          AS add_this_as_alias,
# MAGIC   duplicate_of                             AS for_this_canonical_vendor,
# MAGIC   ROUND(duplicate_score * 100, 1)          AS match_pct,
# MAGIC   duplicate_gate,
# MAGIC   status,
# MAGIC   verdict,
# MAGIC   orgs_count,
# MAGIC   events,
# MAGIC   official_name                            AS ai_official_name,
# MAGIC   enum_name,
# MAGIC   jira_ticket,
# MAGIC   verified_at
# MAGIC FROM `s3-write-bucket`.`ido`.coralogix_vendor_candidates
# MAGIC WHERE should_add_alias = true
# MAGIC ORDER BY orgs_count DESC NULLS LAST, match_pct DESC

# COMMAND ----------

# MAGIC %sql
# MAGIC -- LEGIT new vendors with ticket status
# MAGIC SELECT
# MAGIC   vendor_name_raw,
# MAGIC   official_name,
# MAGIC   enum_name,
# MAGIC   confidence_score,
# MAGIC   orgs_count,
# MAGIC   jira_ticket,
# MAGIC   supported_protocols,
# MAGIC   device_types,
# MAGIC   website
# MAGIC FROM `s3-write-bucket`.`ido`.coralogix_vendor_candidates
# MAGIC WHERE status  = 'COMPLETED'
# MAGIC   AND verdict = 'LEGIT'
# MAGIC ORDER BY orgs_count DESC

# COMMAND ----------

# MAGIC %sql
# MAGIC -- Failed vendors that need attention
# MAGIC SELECT
# MAGIC   vendor_name_raw,
# MAGIC   failure_counter,
# MAGIC   orgs_count,
# MAGIC   pipeline_run_id,
# MAGIC   last_seen
# MAGIC FROM `s3-write-bucket`.`ido`.coralogix_vendor_candidates
# MAGIC WHERE status = 'FAILED'
# MAGIC ORDER BY failure_counter DESC, orgs_count DESC
