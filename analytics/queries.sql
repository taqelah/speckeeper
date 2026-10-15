-- Saved queries over the "why?" agent's analytics events in BigQuery.
-- The table is written by ADK's BigQueryAgentAnalyticsPlugin (src/speckeeper/analytics.py): one row per
-- event, with token counts in attributes.usage_metadata and timings in latency_ms. It holds no message
-- text. Each query reads only the last 30 days.
--
-- Run one: bq query --project_id=speckeeper-taqelah --use_legacy_sql=false < analytics/queries.sql
-- or paste a single query into the BigQuery console.


-- 1. Cost per question. Every Gemini call in one question (an invocation), priced at list prices on
--    Oct 16, 2026, in USD per million tokens: Flash input 0.75, output and thinking 3.75; Pro (the
--    fallback) input 2.00, output and thinking 12.00.
WITH calls AS (
  SELECT
    invocation_id,
    IFNULL(CAST(JSON_VALUE(attributes, '$.usage_metadata.prompt_token_count') AS INT64), 0) AS input_tokens,
    IFNULL(CAST(JSON_VALUE(attributes, '$.usage_metadata.candidates_token_count') AS INT64), 0)
      + IFNULL(CAST(JSON_VALUE(attributes, '$.usage_metadata.thoughts_token_count') AS INT64), 0) AS output_tokens,
    CONTAINS_SUBSTR(IFNULL(JSON_VALUE(attributes, '$.model_version'), ''), 'pro') AS on_pro
  FROM `speckeeper-taqelah.speckeeper_analytics.agent_events`
  WHERE event_type = 'LLM_RESPONSE' AND timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
),
questions AS (
  SELECT
    invocation_id,
    COUNT(*) AS model_calls,
    SUM(input_tokens + output_tokens) AS tokens,
    SUM(IF(on_pro, input_tokens * 2.00 + output_tokens * 12.00, input_tokens * 0.75 + output_tokens * 3.75)) / 1e6 AS usd
  FROM calls
  GROUP BY invocation_id
)
SELECT
  COUNT(*) AS questions,
  ROUND(APPROX_QUANTILES(usd, 2)[OFFSET(1)], 4) AS median_usd,
  ROUND(MAX(usd), 4) AS max_usd,
  ROUND(SUM(usd), 4) AS total_usd,
  APPROX_QUANTILES(tokens, 2)[OFFSET(1)] AS median_tokens,
  ROUND(AVG(model_calls), 1) AS avg_model_calls
FROM questions;


-- 2. Latency per step: a whole question, each Gemini call and each tool call (milliseconds).
SELECT
  CASE event_type
    WHEN 'INVOCATION_COMPLETED' THEN 'whole question'
    WHEN 'LLM_RESPONSE' THEN 'Gemini call'
    WHEN 'TOOL_COMPLETED' THEN 'tool call'
  END AS step,
  COUNT(*) AS n,
  APPROX_QUANTILES(CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64), 100)[OFFSET(50)] AS p50_ms,
  APPROX_QUANTILES(CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64), 100)[OFFSET(95)] AS p95_ms,
  MAX(CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64)) AS max_ms
FROM `speckeeper-taqelah.speckeeper_analytics.agent_events`
WHERE event_type IN ('INVOCATION_COMPLETED', 'LLM_RESPONSE', 'TOOL_COMPLETED')
  AND timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
GROUP BY step
ORDER BY p50_ms DESC;


-- 3. Tool mix: which lookups the agent makes, how often they find nothing, and how long they take.
SELECT
  JSON_VALUE(content, '$.tool') AS tool,
  COUNT(*) AS calls,
  COUNTIF(JSON_VALUE(content, '$.found') = 'false') AS found_nothing,
  APPROX_QUANTILES(CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64), 100)[OFFSET(50)] AS p50_ms
FROM `speckeeper-taqelah.speckeeper_analytics.agent_events`
WHERE event_type = 'TOOL_COMPLETED' AND timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
GROUP BY tool
ORDER BY calls DESC;
