# Coralogix Vendor Verifier pipeline

Git source of truth for the batch pipeline (Wave 1). Deployed to Databricks workspace
paths — not to the Streamlit Databricks App.

| Repo path | Workspace path |
|-----------|----------------|
| `pipeline/coralogix_vendor_pipeline.py` | `/Workspace/Users/ido.m@claroty.com/coralogix_vendor_pipeline` |
| `pipeline/vendor_verifier_*.py` | `/Workspace/Users/ido.m@claroty.com/vendor-verifier-pipeline-lib/` |

## Batch-only tickets and PRs (Sep 2026)

The pipeline can no longer produce a single-vendor NET ticket or PR — that path belongs
to the interactive Device Labeler Vendor Verifier tab.

- `CREATE_TICKETS` creates **one** batch NET ticket per run
  (`[Vendor Verifier] Batch: N new vendor(s) + M alias(es)`), listing every qualifying
  vendor and alias. All rows in the batch share that `jira_ticket`.
- `CREATE_PR` reuses the batch ticket as the PR's lead ticket, and links the PR once per
  distinct ticket instead of once per row.
- `MIN_BATCH_SIZE` (default **2**, hard floor 2) gates both stages. Fewer qualifying rows
  → nothing is created; the rows keep `jira_ticket IS NULL` / `pr_url IS NULL` and roll
  into the next run. Setting `MIN_BATCH_SIZE = 1` raises at config time, and
  `create_batch_pr` raises if called with a smaller batch.
- The ticket description lists up to `TICKET_MAX_LISTED` (60) vendors, reserving
  `TICKET_ALIAS_RESERVE` (15) slots for aliases, then adds a "… and N more" pointer.
  Section headers always show true totals. This keeps an `all_pending` backlog batch
  under Jira's ~32k description limit — the PR body and Delta hold the full list.

## P1 verdict taxonomy (Aug 2026)

- Gemini verdicts: `LEGIT`, `NOT-MANUFACTURER`, `BRAND-OF`, `AMBIGUOUS`, `GENERIC`,
  `SOFTWARE-ONLY`, `SUSPICIOUS`
- Auto-PR / Jira for **new** vendors only when `LEGIT` + original manufacturer +
  `distinct_companies_found == 1` + HIGH/MEDIUM confidence
- `AMBIGUOUS` (and other non-LEGIT) stay on `coralogix_vendor_candidates` — no Jira/PR;
  Lakeview triage section filters `verdict = 'AMBIGUOUS'`
- Columns: `distinct_companies_found`, `alternative_companies`, `is_original_manufacturer`,
  `parent_company`

## P2 parent-brand resolution (Aug 2026)

- On `BRAND-OF` (or LEGIT with `parent_company` / `acquired_by`), look up parent in registry
  via `resolve_parent_for_brand_of` (`vendor_verifier_similarity.py`).
- Parent found → `status=DUPLICATE`, `should_add_alias=true`, `duplicate_gate=p2-parent`.
- Parent absent → keep COMPLETED non-PR verdict (dashboard / curator).

## Safe fixes in this tree (P3 / P4 / P6)

- **P3** — `vendor_verifier_similarity.find_similar`: max(fuzzy, word-overlap), stopwords,
  punctuation fold, length-aware Dell/Bell cap
- **P4** — EXTRACT sanitize / control-byte ghost skip
- **P6** — Gate 2 `should_add_alias` only when `verdict == "LEGIT"`

Post-mortem: [VENDOR_VERIFIER_WEAKPOINTS.md](VENDOR_VERIFIER_WEAKPOINTS.md)

## Deploy (Databricks profile `claroty`)

```bash
WS="/Workspace/Users/ido.m@claroty.com"
REPO_ROOT="$(git rev-parse --show-toplevel)"

for f in vendor_verifier_normalization.py vendor_verifier_batch_pr.py \
         vendor_verifier_jira.py vendor_verifier_similarity.py; do
  databricks workspace import "$WS/vendor-verifier-pipeline-lib/$f" \
    --file "$REPO_ROOT/pipeline/$f" --format AUTO --overwrite -p claroty
done

databricks workspace import "$WS/coralogix_vendor_pipeline" \
  --file "$REPO_ROOT/pipeline/coralogix_vendor_pipeline.py" \
  --format SOURCE --language PYTHON --overwrite -p claroty
```

## Tests

```bash
pip install pytest
pytest test_vendor_verifier_similarity.py -v
```
