-- Exploratory queries for `lily123.directdata.directtable`
-- Looking for unexpected patterns, not necessarily problems.

-- 1. Do industry-sponsored trials publish results more or less than others?
-- (Academia is assumed more transparent, but industry sponsors are usually
-- the ones under FDAAA reporting mandates.)
SELECT
  lead_sponsor_class,
  COUNTIF(has_results) / COUNT(*) AS pct_with_results,
  COUNT(*) AS n
FROM `lily123.directdata.directtable`
GROUP BY lead_sponsor_class
ORDER BY n DESC;

-- 2. "Healthy volunteers allowed" for serious conditions.
-- Usually signals a PK/safety-only arm; the specific conditions that show
-- up here are often surprising.
SELECT
  c AS condition,
  COUNT(*) AS n
FROM `lily123.directdata.directtable`, UNNEST(conditions) AS c
WHERE healthy_volunteers = TRUE
GROUP BY c
ORDER BY n DESC
LIMIT 25;

-- 3. Single-age eligibility windows.
-- Trials that enroll only people of one exact age (min == max).
SELECT
  nct_id,
  brief_title,
  minimum_age_days,
  sex
FROM `lily123.directdata.directtable`
WHERE minimum_age_days = maximum_age_days
  AND minimum_age_days > 0;

-- 4. Intervention type mix by decade.
-- Watch for partial start_date values ("2007-07", "2006") when parsing.
SELECT
  EXTRACT(YEAR FROM PARSE_DATE('%Y-%m', start_date)) AS start_year,
  it AS intervention_type,
  COUNT(*) AS n
FROM `lily123.directdata.directtable`, UNNEST(intervention_types) AS it
WHERE REGEXP_CONTAINS(start_date, r'^\d{4}-\d{2}$')
GROUP BY start_year, intervention_type
ORDER BY start_year, n DESC;

-- 5. Sponsor concentration / power law.
-- Compare the top-20 sum against total row count.
SELECT
  lead_sponsor,
  COUNT(*) AS n
FROM `lily123.directdata.directtable`
GROUP BY lead_sponsor
ORDER BY n DESC
LIMIT 20;

-- 6. Enrollment size vs. phase -- inversions.
-- Phase 1 trials with unusually large enrollment.
SELECT
  nct_id,
  brief_title,
  phases,
  enrollment_count
FROM `lily123.directdata.directtable`, UNNEST(phases) AS phase
WHERE phase = 'PHASE1'
  AND enrollment_count > 1000
ORDER BY enrollment_count DESC;

-- 7. Sex-restricted trials for conditions that aren't obviously
-- reproductive/sex-linked. Adjust the excluded term list as needed.
SELECT
  sex,
  c AS condition,
  COUNT(*) AS n
FROM `lily123.directdata.directtable`, UNNEST(conditions) AS c
WHERE sex != 'ALL'
  AND NOT REGEXP_CONTAINS(
    LOWER(c),
    r'pregnan|prostate|ovarian|uterine|cervical|testic|menopaus|breast|gynecolog'
  )
GROUP BY sex, condition
ORDER BY n DESC
LIMIT 25;

-- 8. Data-quality quirk: phases on observational studies.
-- Phases are an interventional-trial concept; any hits here are a
-- labeling artifact in the source data.
SELECT
  nct_id,
  study_type,
  phases
FROM `lily123.directdata.directtable`
WHERE study_type = 'OBSERVATIONAL'
  AND ARRAY_LENGTH(phases) > 0;

-- 9. Trials still "RECRUITING" that started years ago.
-- Legitimate for long natural-history studies, but a chunk of these are
-- just stale listings nobody updated.
SELECT
  nct_id,
  brief_title,
  start_date,
  overall_status
FROM `lily123.directdata.directtable`
WHERE overall_status = 'RECRUITING'
  AND REGEXP_CONTAINS(start_date, r'^\d{4}')
  AND CAST(SUBSTR(start_date, 1, 4) AS INT64) <= EXTRACT(YEAR FROM CURRENT_DATE()) - 5
ORDER BY start_date;

-- 10. The full spread of overall_status values.
-- Most people only think of RECRUITING/COMPLETED; the tail
-- (WITHDRAWN, SUSPENDED, APPROVED_FOR_MARKETING, UNKNOWN, ...) is where
-- the interesting edge cases live.
SELECT
  overall_status,
  COUNT(*) AS n
FROM `lily123.directdata.directtable`
GROUP BY overall_status
ORDER BY n DESC;

-- 11. Near-zero enrollment trials.
-- enrollment_count of 0 or 1 -- single-subject case studies masquerading
-- as clinical trials, or data-entry artifacts.
SELECT
  nct_id,
  brief_title,
  start_date,
  study_type,
  enrollment_count
FROM `lily123.directdata.directtable`
WHERE enrollment_count <= 1
ORDER BY enrollment_count;

-- 12. Trials juggling an unusually large number of conditions at once.
-- Most trials target 1-2 conditions; the outliers are worth a look.
SELECT
  nct_id,
  brief_title,
  ARRAY_LENGTH(conditions) AS num_conditions,
  conditions
FROM `lily123.directdata.directtable`
ORDER BY num_conditions DESC
LIMIT 25;

-- 13. Title length vs. sponsor class.
-- Do industry titles trend shorter/branded while academic titles trend
-- longer/descriptive, or is that stereotype wrong?
SELECT
  lead_sponsor_class,
  AVG(LENGTH(official_title)) AS avg_official_title_len,
  AVG(LENGTH(brief_title)) AS avg_brief_title_len,
  COUNT(*) AS n
FROM `lily123.directdata.directtable`
WHERE official_title IS NOT NULL
GROUP BY lead_sponsor_class
ORDER BY n DESC;

-- 14. Tech-buzzword adoption curve.
-- Track mentions of newer modalities in brief_summary over start year --
-- the year each term starts appearing, and how fast it grows.
SELECT
  EXTRACT(YEAR FROM PARSE_DATE('%Y-%m', start_date)) AS start_year,
  COUNTIF(REGEXP_CONTAINS(LOWER(brief_summary), r'artificial intelligence|machine learning')) AS ai_ml_mentions,
  COUNTIF(REGEXP_CONTAINS(LOWER(brief_summary), r'wearable')) AS wearable_mentions,
  COUNTIF(REGEXP_CONTAINS(LOWER(brief_summary), r'telehealth|telemedicine')) AS telehealth_mentions,
  COUNT(*) AS total_trials
FROM `lily123.directdata.directtable`
WHERE REGEXP_CONTAINS(start_date, r'^\d{4}-\d{2}$')
GROUP BY start_year
ORDER BY start_year;

-- 15. Mining the full JSON payload: MeSH ancestor terms that never show up
-- in the trial's own plain-language conditions/keywords.
-- This digs into `trial` (the raw JSON column) since ancestor MeSH terms
-- aren't promoted to flat columns.
SELECT
  nct_id,
  brief_title,
  conditions,
  ARRAY(
    SELECT JSON_VALUE(term, '$.term')
    FROM UNNEST(JSON_EXTRACT_ARRAY(trial, '$.derivedSection.conditionBrowseModule.ancestors')) AS term
  ) AS mesh_ancestor_terms
FROM `lily123.directdata.directtable`
WHERE JSON_EXTRACT_ARRAY(trial, '$.derivedSection.conditionBrowseModule.ancestors') IS NOT NULL
LIMIT 25;

-- 16. Phase-progression funnel by inferred "drug program" (heuristic).
-- ClinicalTrials.gov has no official field linking a Phase 1 trial to its
-- Phase 2/3 successor -- each phase is almost always filed as a separate
-- NCT ID. This approximates a "program" by normalizing the DRUG
-- intervention name (stripping dosage/formulation notes and dropping
-- placebo/vehicle/sham arms) and grouping by (drug name, lead sponsor,
-- condition set). A program's "max phase reached" stands in for how far
-- it progressed. Treat this as directional, not exact: drug renames,
-- sponsor acquisitions, and combined "PHASE1/PHASE2" trials will cause
-- both over- and under-counting. Sanity-check with query 17 before
-- trusting the numbers.
WITH drug_interventions AS (
  SELECT
    nct_id,
    lead_sponsor,
    conditions,
    phases,
    start_date,
    TRIM(REGEXP_REPLACE(
      REGEXP_REPLACE(
        REGEXP_REPLACE(LOWER(i.name), r'\([^)]*\)', ''),
        r'\b\d+(\.\d+)?\s*(mg|mcg|g|ml|iu|%)\b', ''
      ),
      r'\s+', ' '
    )) AS drug_key
  FROM `lily123.directdata.directtable`, UNNEST(interventions) AS i
  WHERE i.type = 'DRUG'
    AND i.name IS NOT NULL
    AND NOT REGEXP_CONTAINS(LOWER(i.name), r'placebo|vehicle|sham')
),

program_phase AS (
  SELECT
    drug_key,
    lead_sponsor,
    ARRAY_TO_STRING(ARRAY(SELECT c FROM UNNEST(conditions) c ORDER BY c), '|') AS condition_key,
    phase
  FROM drug_interventions, UNNEST(phases) AS phase
  WHERE drug_key != ''
    AND phase IN ('EARLY_PHASE1', 'PHASE1', 'PHASE2', 'PHASE3', 'PHASE4')
  GROUP BY drug_key, lead_sponsor, condition_key, phase
),

program_max_phase AS (
  SELECT
    drug_key,
    lead_sponsor,
    condition_key,
    MAX(CASE phase
      WHEN 'EARLY_PHASE1' THEN 0
      WHEN 'PHASE1' THEN 1
      WHEN 'PHASE2' THEN 2
      WHEN 'PHASE3' THEN 3
      WHEN 'PHASE4' THEN 4
    END) AS max_phase_ord
  FROM program_phase
  GROUP BY drug_key, lead_sponsor, condition_key
),

funnel AS (
  SELECT
    phase_ord,
    phase_name,
    COUNTIF(pmp.max_phase_ord >= phase_ord) AS programs_reaching_this_phase_or_later
  FROM program_max_phase pmp
  CROSS JOIN UNNEST([
    STRUCT(0 AS phase_ord, 'EARLY_PHASE1' AS phase_name),
    STRUCT(1, 'PHASE1'),
    STRUCT(2, 'PHASE2'),
    STRUCT(3, 'PHASE3'),
    STRUCT(4, 'PHASE4')
  ])
  GROUP BY phase_ord, phase_name
)

SELECT
  phase_ord,
  phase_name,
  programs_reaching_this_phase_or_later,
  -- Share of all programs (i.e. of everyone who started at EARLY_PHASE1-or-later).
  ROUND(SAFE_DIVIDE(
    programs_reaching_this_phase_or_later,
    FIRST_VALUE(programs_reaching_this_phase_or_later) OVER (ORDER BY phase_ord)
  ), 3) AS pct_of_total,
  -- Stage-over-stage conversion rate (e.g. of Phase 1 programs, what % also hit Phase 2).
  ROUND(SAFE_DIVIDE(
    programs_reaching_this_phase_or_later,
    LAG(programs_reaching_this_phase_or_later) OVER (ORDER BY phase_ord)
  ), 3) AS pct_of_previous_phase
FROM funnel
ORDER BY phase_ord;

-- 17. Sanity check for query 16's grouping heuristic.
-- Inspect the programs that appear to span the most phases, to eyeball
-- whether the drug_key normalization is merging/splitting things sensibly
-- (e.g. two unrelated drugs colliding on a stripped-down name, or the same
-- drug failing to match itself across trials) before trusting query 16.
WITH drug_interventions AS (
  SELECT
    nct_id,
    lead_sponsor,
    conditions,
    phases,
    TRIM(REGEXP_REPLACE(
      REGEXP_REPLACE(
        REGEXP_REPLACE(LOWER(i.name), r'\([^)]*\)', ''),
        r'\b\d+(\.\d+)?\s*(mg|mcg|g|ml|iu|%)\b', ''
      ),
      r'\s+', ' '
    )) AS drug_key
  FROM `lily123.directdata.directtable`, UNNEST(interventions) AS i
  WHERE i.type = 'DRUG'
    AND i.name IS NOT NULL
    AND NOT REGEXP_CONTAINS(LOWER(i.name), r'placebo|vehicle|sham')
)
SELECT
  drug_key,
  lead_sponsor,
  ARRAY_TO_STRING(ARRAY(SELECT c FROM UNNEST(conditions) c ORDER BY c), '|') AS condition_key,
  ARRAY_AGG(DISTINCT phase IGNORE NULLS) AS phases_seen,
  ARRAY_AGG(DISTINCT nct_id) AS nct_ids,
  COUNT(DISTINCT phase) AS distinct_phase_count
FROM drug_interventions, UNNEST(phases) AS phase
WHERE drug_key != ''
GROUP BY drug_key, lead_sponsor, condition_key
HAVING distinct_phase_count > 1
ORDER BY distinct_phase_count DESC
LIMIT 25;

-- 18. Visibility into "NA-only" drug programs excluded from query 16's funnel.
-- Query 16 filters out phase = 'NA' entirely, so a program whose every
-- trial is phase-NA (e.g. supplement or behavioral-adjacent drug studies
-- that don't require FDA phase designation) never shows up in that funnel
-- at all -- not even as a zero. This surfaces how many programs that is,
-- so the funnel's silence isn't mistaken for "no such programs exist."
WITH drug_interventions AS (
  SELECT
    nct_id,
    lead_sponsor,
    conditions,
    phases,
    TRIM(REGEXP_REPLACE(
      REGEXP_REPLACE(
        REGEXP_REPLACE(LOWER(i.name), r'\([^)]*\)', ''),
        r'\b\d+(\.\d+)?\s*(mg|mcg|g|ml|iu|%)\b', ''
      ),
      r'\s+', ' '
    )) AS drug_key
  FROM `lily123.directdata.directtable`, UNNEST(interventions) AS i
  WHERE i.type = 'DRUG'
    AND i.name IS NOT NULL
    AND NOT REGEXP_CONTAINS(LOWER(i.name), r'placebo|vehicle|sham')
),

program_phases AS (
  SELECT
    drug_key,
    lead_sponsor,
    ARRAY_TO_STRING(ARRAY(SELECT c FROM UNNEST(conditions) c ORDER BY c), '|') AS condition_key,
    ARRAY_AGG(DISTINCT phase IGNORE NULLS) AS phases_seen
  FROM drug_interventions, UNNEST(phases) AS phase
  WHERE drug_key != ''
  GROUP BY drug_key, lead_sponsor, condition_key
)

SELECT
  COUNT(*) AS total_programs,
  COUNTIF(
    ARRAY_LENGTH(ARRAY(
      SELECT p FROM UNNEST(phases_seen) p
      WHERE p IN ('EARLY_PHASE1', 'PHASE1', 'PHASE2', 'PHASE3', 'PHASE4')
    )) = 0
  ) AS na_only_programs,
  COUNTIF(
    ARRAY_LENGTH(ARRAY(
      SELECT p FROM UNNEST(phases_seen) p
      WHERE p IN ('EARLY_PHASE1', 'PHASE1', 'PHASE2', 'PHASE3', 'PHASE4')
    )) > 0
  ) AS programs_with_a_real_phase,
  ROUND(
    COUNTIF(
      ARRAY_LENGTH(ARRAY(
        SELECT p FROM UNNEST(phases_seen) p
        WHERE p IN ('EARLY_PHASE1', 'PHASE1', 'PHASE2', 'PHASE3', 'PHASE4')
      )) = 0
    ) / COUNT(*),
    3
  ) AS pct_na_only
FROM program_phases;

-- 19. Average/median time to progress from one phase to the next.
-- Same drug-program grouping heuristic as query 16 (see its header for
-- caveats). For each program, takes the earliest start_date seen at each
-- phase, then measures the gap to the next phase's earliest start_date.
-- Only counts a transition when the later phase's date is on or after the
-- earlier phase's date -- "backwards" pairs are dropped rather than
-- averaged in as negative durations, since those usually mean the
-- drug_key grouping merged two unrelated programs rather than a real
-- inversion. start_date is often only "YYYY" or "YYYY-MM" in the source
-- data, so day-level precision here is approximate.
WITH drug_interventions AS (
  SELECT
    nct_id,
    lead_sponsor,
    conditions,
    phases,
    start_date,
    TRIM(REGEXP_REPLACE(
      REGEXP_REPLACE(
        REGEXP_REPLACE(LOWER(i.name), r'\([^)]*\)', ''),
        r'\b\d+(\.\d+)?\s*(mg|mcg|g|ml|iu|%)\b', ''
      ),
      r'\s+', ' '
    )) AS drug_key
  FROM `lily123.directdata.directtable`, UNNEST(interventions) AS i
  WHERE i.type = 'DRUG'
    AND i.name IS NOT NULL
    AND NOT REGEXP_CONTAINS(LOWER(i.name), r'placebo|vehicle|sham')
),

program_phase_dates AS (
  SELECT
    drug_key,
    lead_sponsor,
    ARRAY_TO_STRING(ARRAY(SELECT c FROM UNNEST(conditions) c ORDER BY c), '|') AS condition_key,
    phase,
    MIN(CASE
      WHEN REGEXP_CONTAINS(start_date, r'^\d{4}-\d{2}-\d{2}$') THEN PARSE_DATE('%Y-%m-%d', start_date)
      WHEN REGEXP_CONTAINS(start_date, r'^\d{4}-\d{2}$') THEN PARSE_DATE('%Y-%m', start_date)
      WHEN REGEXP_CONTAINS(start_date, r'^\d{4}$') THEN PARSE_DATE('%Y', start_date)
    END) AS earliest_start
  FROM drug_interventions, UNNEST(phases) AS phase
  WHERE drug_key != ''
    AND phase IN ('EARLY_PHASE1', 'PHASE1', 'PHASE2', 'PHASE3', 'PHASE4')
  GROUP BY drug_key, lead_sponsor, condition_key, phase
),

program_phase_pivot AS (
  SELECT
    drug_key,
    lead_sponsor,
    condition_key,
    MAX(IF(phase = 'EARLY_PHASE1', earliest_start, NULL)) AS early_phase1_date,
    MAX(IF(phase = 'PHASE1', earliest_start, NULL)) AS phase1_date,
    MAX(IF(phase = 'PHASE2', earliest_start, NULL)) AS phase2_date,
    MAX(IF(phase = 'PHASE3', earliest_start, NULL)) AS phase3_date,
    MAX(IF(phase = 'PHASE4', earliest_start, NULL)) AS phase4_date
  FROM program_phase_dates
  GROUP BY drug_key, lead_sponsor, condition_key
),

transitions AS (
  SELECT 0 AS ordinal, 'EARLY_PHASE1 -> PHASE1' AS transition,
         DATE_DIFF(phase1_date, early_phase1_date, DAY) AS days
  FROM program_phase_pivot
  WHERE early_phase1_date IS NOT NULL AND phase1_date IS NOT NULL
    AND phase1_date >= early_phase1_date

  UNION ALL

  SELECT 1, 'PHASE1 -> PHASE2', DATE_DIFF(phase2_date, phase1_date, DAY)
  FROM program_phase_pivot
  WHERE phase1_date IS NOT NULL AND phase2_date IS NOT NULL
    AND phase2_date >= phase1_date

  UNION ALL

  SELECT 2, 'PHASE2 -> PHASE3', DATE_DIFF(phase3_date, phase2_date, DAY)
  FROM program_phase_pivot
  WHERE phase2_date IS NOT NULL AND phase3_date IS NOT NULL
    AND phase3_date >= phase2_date

  UNION ALL

  SELECT 3, 'PHASE3 -> PHASE4', DATE_DIFF(phase4_date, phase3_date, DAY)
  FROM program_phase_pivot
  WHERE phase3_date IS NOT NULL AND phase4_date IS NOT NULL
    AND phase4_date >= phase3_date
)

SELECT
  ordinal,
  transition,
  COUNT(*) AS n_programs,
  ROUND(AVG(days) / 30.44, 1) AS avg_months,
  ROUND(APPROX_QUANTILES(days, 2)[OFFSET(1)] / 30.44, 1) AS median_months,
  ROUND(AVG(days) / 365.25, 2) AS avg_years
FROM transitions
GROUP BY ordinal, transition
ORDER BY ordinal;
