# Coralogix Vendor Verifier pipeline

Git source of truth for the batch pipeline (Wave 1). Deployed to Databricks workspace
paths — not to the Streamlit Databricks App.

| Repo path | Workspace path |
|-----------|----------------|
| `pipeline/coralogix_vendor_pipeline.py` | `/Workspace/Users/ido.m@claroty.com/coralogix_vendor_pipeline` |
| `pipeline/vendor_verifier_*.py` | `/Workspace/Users/ido.m@claroty.com/vendor-verifier-pipeline-lib/` |

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
