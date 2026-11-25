-- Proposed replacement for the 'Computing savings yourself' query in weave-os/router docs/ANALYTICS_EXPORT.md
WITH tokens AS (
    SELECT d.*,
           COALESCE(d.cache_creation_tokens, 0) AS cache_write,
           COALESCE(d.cache_read_tokens, 0)     AS cache_read,
           GREATEST(
             CASE WHEN d.decision_provider IN ('anthropic', 'anthropic_gateway', 'wafer_anthropic')
                  THEN d.input_tokens
                  ELSE d.input_tokens - COALESCE(d.cache_creation_tokens, 0) - COALESCE(d.cache_read_tokens, 0)
             END, 0)                            AS fresh_input
    FROM routing_decisions d
    WHERE d.recorded_at >= now() - interval '30 days'
      AND d.api_rate_input_cost_usd IS NOT NULL
), priced AS (
    SELECT t.api_rate_input_cost_usd + t.api_rate_output_cost_usd AS served_usd,
           (t.fresh_input
            + t.cache_write * p.cache_write_multiplier
            + t.cache_read  * p.cache_read_multiplier) / 1e6 * p.input_usd_per_1m
         + (t.output_tokens / 1e6) * p.output_usd_per_1m          AS baseline_usd
    FROM tokens t
    -- baseline_bindings(model, provider): one row per baseline model, naming the
    -- provider you would otherwise have used, e.g. ('claude-opus-5-5', 'anthropic')
    JOIN baseline_bindings b
      ON b.model = COALESCE(t.blind_experiment_baseline_model, t.requested_model)
    JOIN model_prices p ON p.model = b.model AND p.provider = b.provider
)
SELECT sum(baseline_usd - served_usd) AS savings_usd FROM priced;
