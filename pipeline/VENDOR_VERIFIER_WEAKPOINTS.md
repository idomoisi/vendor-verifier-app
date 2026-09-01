# Vendor Verifier — Pipeline Weak Points (mega-batch post-mortem)

**Scope:** Coralogix pipeline run `336f2675` → NET-14592 → PR
[medigateio/medigator#50654](https://github.com/medigateio/medigator/pull/50654).

**Evidence base**

| Source | What it gives |
|---|---|
| `notebooks/databricks/.vendor_review_queue.json` | 80 candidates with curator `_decision` / `_flags` |
| NET-14592 description | Final curated split: 39 new vendors, ~21 aliases, 20 skipped |
| Team analyst verdict sheet (Jul 27) | Human verdict on each of the 39 new vendors |
| `coralogix_vendor_pipeline.py` (= deployed copy, byte-identical) | Gate + prompt implementation |
| `medigator/common/domain_model/xiot/device.py` | Real registry: 10,692 display names (3,129 first-class / 7,563 Manuf) |

---

## Headline

**43 of 80 leads (54%) needed human correction before they were safe to merge.**

| Correction | Count | Who caught it |
|---|---:|---|
| Dropped as not a real vendor lead | 20 | Curator (pre-PR) |
| New vendor → should be alias/promote of an existing entry | 8 | Curator (during PR authoring) |
| New vendor → rejected or needs more data | 15 | Analyst team (post-PR) |

The pipeline's own gates surfaced **1** of those 43 correctly (`WINCOR NIXDORF` → `Diebold Nixdorf`).

Critically, this is not mostly an AI-quality problem. In **13 of the 15** analyst rejections, Gemini
had already written the disqualifying fact into `analyst_note` — and still returned
`verdict = LEGIT`, `confidence = HIGH`, because the response schema has no way to say
*"real company, but not a first-class hardware vendor."*

---

## Finding 1 — The verdict schema cannot express "not a first-class vendor" (13/15 rejections)

`VALID_VERDICTS = {"LEGIT", "SOFTWARE-ONLY", "SUSPICIOUS"}`. When Gemini finds a distributor, a
systems integrator, a retailer, a rebranded subsidiary, or a name shared by two companies, none of
the three values fit. It picks `LEGIT` and puts the caveat in free text that nothing reads.

Verbatim, from the rows the team rejected — all `LEGIT`, all `HIGH` except Oitech:

| Enum | Team verdict | Gemini's own `analyst_note` |
|---|---|---|
| `MEDION` | Need more data | "subsidiary of Lenovo … **there is no evidence of them producing OT** … hardware" |
| `PCSpecialist` | Not a Vendor | "system integrator that builds custom PCs … **not the original manufacturer**" |
| `ATComputers` | Suspicious | "major **IT distributor** in the Czech Republic … private label brands" |
| `TICNOVA` | Not a Vendor | "large Spanish **retail group** for consumer electronics" |
| `SiliconMechanics` | Not a Vendor | "in 2018 it was **acquired by Source Code, LLC** … integrated into … Thinkmate" |
| `Quanmax` | Suspicious | "**merged** with S&T … **renamed S&T AG** … 'Kontron' is maintained as a brand" |
| `PCWARE` | Not a Vendor | "PCWARE is a **brand of** Digitron da Amazônia …" |
| `Intercomp` | Need more data | "**Two distinct companies** named INTERCOMP were identified" |
| `TAROX` | Need more data | "identified **two distinct companies**" |
| `Yanling` | Need more data | "also appears to be **associated with** 'Shenzhen Xin Secco' and 'Iwill' … white-label" |
| `Oitech` | Suspicious | "seems to be a **widely OEM'd or white-labeled** 4U industrial chassis" |
| `SimplyNUC` | Legit with note | "global **systems integrator** and OEM" |
| `NimoDirect` | Legit with note | "designs and **assembles** … components manufactured by Luxshare" |

Only two rejections were genuine model misses with no latent signal: `Lambda` (did not notice the
name collision) and `Trigkey` (did not surface the Shenzhen AZW parent).

**The `MEDION` row is the clearest statement of the defect:** the prompt asks *"Verify if this vendor
manufactures physical, network-connected OT or Medical hardware"*, the model answers *"no evidence"*
in prose, and the schema field says `LEGIT / HIGH`.

## Finding 2 — Confidence gates suitability, but only measures research certainty

`AUTO_PR_CONFIDENCES = {"HIGH", "MEDIUM"}` is the only quality gate between VERIFY and CREATE_PR.
Of the 15 analyst rejections, **14 were `HIGH`** and 1 was `MEDIUM`. Confidence tracks how sure the
model is about the facts it found, not whether those facts qualify the vendor. It has no
discriminating power here and must not be the last gate.

## Finding 3 — No parent-brand resolution, and the parents are already in `device.py`

The Streamlit app has `find_parent_brand_match`; the pipeline has no equivalent. Looking up the
parents Gemini itself named:

| Lead | Parent named in the note | Already in `device.py`? |
|---|---|---|
| `Quanmax` | Kontron | **Yes** — `Kontron`, first-class |
| `MEDION` | Lenovo | **Yes** — `Lenovo`, first-class |
| `Trigkey` | Shenzhen AZW Technology | **Yes** — `ShenzhenAzwTechnology`, first-class |
| `SimplyNUC` | SNUC Systems | No |
| `NimoDirect` | Luxshare | `LuxsharePrecisionIndustry` (Manuf) |

Three rejections resolve automatically with a parent lookup. The same gap produced the eight
alias-downgrades the curator had to make by hand — `Xplore Technologies` and `Motion Computing` both
went to `Zebra`, `J2 Retail / AURES` to `Advantech`, `BCM Advanced Research` to `BcmComputers`.

**Do not gate naively on "has a parent".** Nine of the 24 vendors the team *approved* also carry
parent / white-label / ODM language (`Durabook`, `Dedicated Computing`, `Decenta`, `DOKE`,
`Penguin Computing`, `MOStron`, `Bangho`, `Equus`, `ONERugged`). The rule the team actually applied
is conditional:

> If the parent exists in the registry → emit an alias (or promote). If it does not → the brand
> itself is the correct first-class vendor.

## Finding 4 — `find_similar` scores containment matches with the wrong branch

The fuzzy branch `continue`s before the word-overlap branch is ever reached, so any candidate above
the 0.70 similarity floor is scored by character ratio instead of token containment — and lands
*below* the 0.90 duplicate threshold:

| Input | Existing registry entry | Scored | Should be | Result |
|---|---|---|---|---|
| `Hanwha Techwin America` | `Hanwha Techwin` | 78% fuzzy | 90% word-overlap | missed |
| `Thomas-Krenn.AG` | `Thomas Krenn` | 81% fuzzy | 90% | missed |

At the 0.90 threshold, **zero** of the eight human alias-downgrades were detectable — and the top
match was frequently the wrong company: `Xplore Technologies` → `Pleora Technologies` (89%),
`Exacq Technologies` → `CA Technologies` (85%), `Dell Technologies` → `Bell Technologies` (94%).

## Finding 5 — Single-word overlap scores exactly at the duplicate threshold

The word-overlap branch permits `len(shorter) == 1`, scoring `0.65 + 1.0 * 0.25 = 0.90` — precisely
`DUPLICATE_THRESHOLD`. Any one-word registry entry therefore matches any multi-word input containing
that word. Reproduced against the real registry:

```
'Insyde Software Corp.'                    -> ('Software',    0.90, 'word-overlap')
'Shenzhen RealBom Intelligent Co.'         -> ('Intelligent', 0.90, 'word-overlap')
'TES Touch Embedded Solutions'             -> ('Embedded',    0.90, 'word-overlap')
'Beijing YanYu Intelligent Technology Co.' -> ('Intelligent', 0.90, 'word-overlap')
'BCM Advanced Research'                    -> ('Advanced',    0.90, 'word-overlap')
```

The exposure is structural, not incidental: of 10,692 display names, **4,563 are single tokens** and
**487 of those are ordinary English dictionary words** (`Advanced`, `Automatic`, `Array`, `Apex`,
`Accord`, `Assist`, `Ambient`, `Beacon`, …). Every one is a live false-duplicate trap.

Findings 4 and 5 must be fixed **together** — repairing the short-circuit alone makes the
generic-word hijack strictly worse.

## Finding 6 — Control characters are never stripped, creating phantom vendors

`_merge_channel_frames` applies `.str.strip()`, which removes whitespace but not control bytes. Four
separate Dell candidates survived aggregation — `'\x06ell Inc.'`, `'\x07ell Inc.'`, `'D\x06ll Inc.'`,
`'D\x07ll Inc.'` — together 58 orgs of duplicated noise. One fuzzy-matched to `Bell Technologies` at
94%; another convinced Gemini to invent a railroad-components company called "Dll inc." at MEDIUM
confidence.

## Finding 7 — The pipeline reads a different registry than the app, and has no Manuf/promote path

VERIFY builds `vendor_registry` from `silver.levels_of_classification_per_vendor` plus previously
seen `official_name`s. It never reads `vendors_master` / `vendor_aliases`, and it has no concept of
`VendorSource.Manuf`. Consequences:

- Existing first-class vendors are invisible to the gates (`Hanwha Techwin` was in `device.py` the
  whole time).
- Manuf entries are treated as brand-new vendors instead of promote candidates. Four human
  corrections landed on Manuf rows: `ThomasKrenn`, `BcmComputers`, `IeiIntegration`, plus
  `JetwayInformation` / `InotecSicherheitstechnik` on the skip side.

The Streamlit app already solved this in July (Manuf excluded from Gate 1, strong Manuf match
defaults to promote). The pipeline never received the same change.

## Finding 8 — Gate 2 sets `should_add_alias` before consulting the verdict

`Insyde` came out `verdict=SOFTWARE-ONLY, status=DUPLICATE, should_add_alias=true` and was queued
for an alias PR. The duplicate branch writes the alias flag and `continue`s without ever reading
`result_data["verdict"]`. Gate 1 runs before Gemini so it has no verdict to check — that is
inherent — but Gate 2 does have one and ignores it.

## Finding 9 — No ambiguity detection

Five of the 39 new vendors (`Intercomp`, `Lambda`, `MEDION`, `TAROX`, `Yanling`) are names shared by
multiple real companies. The team's instruction is consistent: *find the exact company and add a
postfix to both the display name and the enum.* The schema returns one `official_name` at
temperature 0.0 and has no field for "how many distinct companies match this string", so an
ambiguous lead is indistinguishable from a clean one.

## Finding 10 — Channel coverage is not what the design assumes

All 80 candidates in this batch carry `channel = "integration"`. Zero `ctd`, zero `lansweeper`,
despite EXTRACT querying all three. Either the other two regexes are not matching in the archive
tier, or this queue was filtered before it was saved. Worth confirming before the weekly job is
unpaused — Wave 1 is supposed to cover all three.

---

## Proposed patches

Priority is by human-corrections-avoided, using this batch as the measure.

### P1 — Extend the verdict schema (addresses 13 of 15 rejections, plus 11 of the 20 skips)

Add to the prompt and `VALID_VERDICTS`:

| Verdict | Meaning | Pipeline action |
|---|---|---|
| `LEGIT` | Original manufacturer of network-connected hardware | new vendor |
| `NOT-MANUFACTURER` | Real company — distributor, reseller, integrator, retailer, assembler | review queue, no PR |
| `BRAND-OF` | Trades under a parent/owner; requires `parent_company` | resolve parent (P2) |
| `AMBIGUOUS` | String matches ≥2 distinct companies; requires `alternative_companies` | same candidates table + dashboard section; no Jira/PR |
| `GENERIC` | Product category, acronym, model number — not a company at all | drop, no PR |
| `SOFTWARE-ONLY` / `SUSPICIOUS` | unchanged | dashboard only |

New structured fields to force the latent signal out of prose:
`is_original_manufacturer` (bool), `parent_company` (str\|null), `acquired_by` (str\|null),
`distinct_companies_found` (int), `alternative_companies` (list).

Auto-PR condition becomes `verdict == "LEGIT" and is_original_manufacturer and
distinct_companies_found == 1`, with confidence demoted to a tiebreaker.

Prompt must state explicitly: *if the input is a product category, acronym, or model number, return
`GENERIC` — do not substitute the most plausible company.* Eleven of the twenty curator skips
(`Mini PC`, `Fanless Mini PC`, `GPU Company`, `RuggedPC`, `O.E.M`, `RDO`, `:3C!`,
`LI1BV-H/LI1BV-LH`, …) exist because the model had no way to refuse.

### P2 — Parent-brand resolution against the real registry (3 rejections + 8 alias-downgrades)

On `BRAND-OF` / non-null `acquired_by`, look the parent up in the registry. Parent found → emit
alias to the parent. Parent absent → keep the brand as a first-class vendor. Port
`find_parent_brand_match` from the app rather than writing a second implementation.

### P3 — Fix `find_similar` (both branches together) — **addressed (ido-moisi-001)**

Implemented in `vendor_verifier_similarity.py` (pipeline-lib + notebook import): compute the
fuzzy ratio and the token-containment score and take the **max** instead of `continue`-ing on
the first hit; require `len(shared) >= 2` for the overlap branch, or full containment of the
shorter token set when the single token is **not** a stopword; normalize punctuation in
`normalize_cmp` (`Thomas-Krenn.AG` → `thomas krenn`); apply a length-aware fuzzy cap so short
near-misses (`Dell` / `Bell`) cannot pass the 0.90 duplicate bar.

### P4 — Sanitize at EXTRACT — **addressed (ido-moisi-001)**

`_merge_channel_frames` strips control characters and zero-width codepoints via
`sanitize_vendor_name_raw` *before* the group-by, then skips control-byte ghosts with
`should_skip_control_ghost` (empty sanitize or corrupt short remnant after control-only delta).

### P5 — Align the registry with the app — **addressed (NET-14892)**

VERIFY now reads `device.py` directly, separates first-class from `VendorSource.Manuf`, attributes
typed + official Manuf matches, and persists `promote` / `promote_rename`. Batch PRs remove the
selected Manuf row and complete rename compatibility edits (`VENDOR_ALIASES` + `oui_info.py`).

### P6 — Gate 2 must respect the verdict — **addressed (ido-moisi-001)**

`should_add_alias` is set only when `gate2_should_add_alias(verdict)` is true (`verdict == "LEGIT"`).
SOFTWARE-ONLY / SUSPICIOUS Gate 2 duplicates no longer queue alias PRs.

---

## Open questions for the team

1. **`Trigkey`** was tagged Legit, but its parent `ShenzhenAzwTechnology` already exists as a
   first-class vendor. By the rule in Finding 3 it should be an alias. Confirm before merge.
2. **`PCWARE`** — the team says distributor; Gemini says it is a brand of Digitron da Amazônia, a
   manufacturer. Which is right determines drop-vs-alias.
3. **`MEDION`** — Gemini says no OT/medical evidence, which would make it out of scope regardless of
   the Lenovo relationship. Is consumer-only hardware in scope for the `Vendor` enum at all?
4. ~~Should `AMBIGUOUS` leads auto-open a research ticket, or stay silent on the dashboard?~~
   **Answered (Aug 2026):** same `coralogix_vendor_candidates` table (`verdict=AMBIGUOUS` +
   `alternative_companies` JSON); **no** auto research ticket; add a Lakeview dashboard section.
   Human curator still picks company + postfix when ready.
5. Finding 10 — is the missing `ctd` / `lansweeper` coverage expected for this batch?
