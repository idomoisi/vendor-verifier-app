# Coralogix Vendor Verifier pipeline

Git source of truth for the batch pipeline (Wave 1). Deployed to Databricks workspace
paths — not to the Streamlit Databricks App.

| Repo path | Workspace path |
|-----------|----------------|
| `pipeline/coralogix_vendor_pipeline.py` | `/Workspace/Users/ido.m@claroty.com/coralogix_vendor_pipeline` |
| `pipeline/vendor_verifier_*.py` | `/Workspace/Users/ido.m@claroty.com/vendor-verifier-pipeline-lib/` |

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

## Manuf promotion (`promote` / `promote_rename`)

- VERIFY loads first-class, `VendorSource.Manuf`, IEEE `resources/manuf`, and `oui_info.py`
  registries separately from medigator `staging`; first-class matches keep duplicate/alias precedence.
- Manuf attribution fuzzy-matches typed + Gemini official names at ≥90%, preferring typed.
- If no enum Manuf matches, cleaned Gemini official names exact-match cleaned IEEE long/short
  identities. Existing first-class OUI mappings become aliases; typed-only matches do not qualify.
- Identity-preserving lifts persist `pr_case=promote`; changed enum or display persists
  `pr_case=promote_rename` with explicit enum/IEEE source metadata.
- Enum-backed batch edits remove the source Manuf row and remap existing OUI references.
  IEEE-only edits insert long + uppercase-short OUI keys without fabricating an enum row.
  Both add `device.py` + `types.py` and conditionally alias the old display.
- A failed vendor edit is omitted from the batch; no partial vendor is included in the PR.

## Deploy (Databricks profile `claroty`)

```bash
WS="/Workspace/Users/ido.m@claroty.com"
REPO_ROOT="$(git rev-parse --show-toplevel)"

for f in vendor_verifier_normalization.py vendor_verifier_batch_pr.py \
         vendor_verifier_jira.py vendor_verifier_similarity.py \
         vendor_verifier_ieee_manuf.py; do
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
PYTHONPATH=pipeline pytest pipeline/test_vendor_verifier_promote_rename.py -v
```
