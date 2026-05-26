-- Snowflake: fake time series → JSON ``p_data`` per entity for DuckDB UDFs / ``invoke_sql_function``.
-- Adjust types/casts for your warehouse. Pass ``invoke_payload`` (or equivalent JSON) to a service
-- that runs DuckDB with ``register_analytical_udfs``.

WITH raw AS (
  SELECT column1 AS entity_id, column2 AS metric_ts, column3 AS metric_val
  FROM VALUES
    (1, '2024-01-01'::TIMESTAMP_NTZ, 10.0),
    (1, '2024-01-02'::TIMESTAMP_NTZ, 20.0),
    (1, '2024-01-03'::TIMESTAMP_NTZ, 15.0),
    (2, '2024-01-01'::TIMESTAMP_NTZ, 100.0),
    (2, '2024-01-02'::TIMESTAMP_NTZ, 110.0),
    (2, '2024-01-03'::TIMESTAMP_NTZ, 105.0)
),
per_point AS (
  SELECT
    entity_id,
    metric_ts,
    OBJECT_CONSTRUCT(
      'time', TO_VARCHAR(metric_ts, 'YYYY-MM-DD"T"HH24:MI:SS.FF3'),
      'value', metric_val
    ) AS pt
  FROM raw
),
agg AS (
  SELECT
    entity_id,
    TO_JSON(
      ARRAY_AGG(pt) WITHIN GROUP (ORDER BY metric_ts)
    ) AS p_data_json
  FROM per_point
  GROUP BY entity_id
)
SELECT
  entity_id,
  p_data_json,
  TO_JSON(
    OBJECT_CONSTRUCT(
      'p_data', PARSE_JSON(p_data_json),
      'p_window_size', 2,
      'p_group_by', ''
    )
  ) AS invoke_payload
FROM agg
ORDER BY entity_id;
