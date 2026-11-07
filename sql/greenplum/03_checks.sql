-- =====================================================================
--  DQ-проверки в исполнении Greenplum: то, что реально уходит в прод.
--  Каждый запрос возвращает ОДНУ строку: metric_value (+ опционально
--  rows_examined / message). Это контракт reliability/checks.py.
--
--  Замечания по производительности (важнее, чем сам SQL):
--   * любая проверка обязана читать партицию, а не всю таблицу:
--     предикат по dt/mth отсекает партиции на этапе планирования;
--   * COUNT(DISTINCT ...) на MPP стоит redistribute motion по ключу -
--     на 500M строк это часы. Поэтому distinct-проверки живут на
--     агрегатах или на dim-таблицах;
--   * сверку fact<->fact лучше строить через FULL JOIN по ключу
--     распределения, иначе планировщик сделает Broadcast на все сегменты.
-- =====================================================================

-- 1. Свежесть загрузки оборотов (P1, SLA 26 часов).
--    Читаем ops.etl_batch, а не max(load_dt) по факту: факт на 40M строк
--    сканируется ~40 c, метаданные - миллисекунды.
SELECT ops.dq_age_hours(MAX(load_dt)) AS metric_value,
       COUNT(*)                       AS rows_examined
  FROM ops.etl_batch
 WHERE entity = 'fct_account_turnover_daily'
   AND status = 'SUCCESS';

-- 2. Свежесть портфеля (P2, SLA 24 часа).
SELECT ops.dq_age_hours(MAX(load_dt)) AS metric_value,
       COUNT(*)                       AS rows_examined
  FROM ops.etl_batch
 WHERE entity = 'fct_loan_portfolio_monthly'
   AND status = 'SUCCESS';

-- 3. Дубликаты гранулярности: одна строка на account_id + dt + currency.
--    GroupAggregate по ключу распределения выполняется локально.
SELECT COALESCE(SUM(cnt - 1), 0) AS metric_value,
       COUNT(*)                  AS rows_examined
  FROM (
        SELECT account_id, dt, currency, COUNT(*) AS cnt
          FROM mart.fct_account_turnover_daily
         WHERE dt >= current_date - 7
         GROUP BY account_id, dt, currency
        HAVING COUNT(*) > 1
       ) d;

-- 4. Сироты: обороты по клиенту, которого нет в справочнике.
--    dim_client реплицирован, значит join не требует motion.
SELECT COUNT(*)                        AS metric_value,
       (SELECT COUNT(*)
          FROM mart.fct_account_turnover_daily
         WHERE dt >= current_date - 7) AS rows_examined
  FROM mart.fct_account_turnover_daily t
  LEFT JOIN mart.dim_client c ON c.client_id = t.client_id
 WHERE t.dt >= current_date - 7
   AND c.client_id IS NULL;

-- 5. Отрицательный исходящий остаток: для расчётного счёта это всегда
--    либо технический овердрафт (значит не учли лимит), либо ошибка знака.
SELECT COUNT(*) AS metric_value,
       (SELECT COUNT(*)
          FROM mart.fct_account_turnover_daily
         WHERE dt >= current_date - 7) AS rows_examined
  FROM mart.fct_account_turnover_daily
 WHERE dt >= current_date - 7
   AND balance_eod < 0;

-- 6. Непрерывность остатка: balance_eod(dt) = balance_eod(dt-1) + credit - debit.
--    LAG по account_id считается локально на сегменте - именно ради этой
--    проверки распределение по account_id, а не по dt.
WITH ordered AS (
    SELECT account_id,
           dt,
           currency,
           balance_eod,
           debit_turnover,
           credit_turnover,
           LAG(balance_eod) OVER (PARTITION BY account_id, currency ORDER BY dt) AS prev_balance
      FROM mart.fct_account_turnover_daily
     WHERE dt >= current_date - 30
)
SELECT COUNT(*) FILTER (
           WHERE prev_balance IS NOT NULL
             AND abs(balance_eod - (prev_balance + credit_turnover - debit_turnover)) > 0.01
       ) AS metric_value,
       COUNT(*) AS rows_examined
  FROM ordered;

-- 7. Аномалия объёма: последний день против медианы предыдущих 7 дней.
--    percentile_cont как оконная функция на Greenplum 6 не работает
--    ("OVER is not supported for ordered-set aggregate percentile_cont"),
--    поэтому медиана считается ранговым способом: берём две средние
--    строки окна и усредняем - результат тот же, стоимость ниже.
WITH daily AS (
    SELECT dt, COUNT(*) AS cnt
      FROM mart.fct_account_turnover_daily
     WHERE dt >= current_date - 15
     GROUP BY dt
),
pairs AS (
    SELECT d.dt, d.cnt, p.cnt AS prev_cnt
      FROM daily d
      JOIN daily p ON p.dt BETWEEN d.dt - 7 AND d.dt - 1
),
ranked AS (
    SELECT dt,
           cnt,
           prev_cnt,
           ROW_NUMBER() OVER (PARTITION BY dt ORDER BY prev_cnt) AS rn,
           COUNT(*)     OVER (PARTITION BY dt)                   AS n
      FROM pairs
),
med AS (
    SELECT dt, AVG(prev_cnt) AS median_prev
      FROM ranked
     WHERE rn IN ((n + 1) / 2, (n + 2) / 2)
     GROUP BY dt
)
SELECT ROUND(r.cnt::numeric / NULLIF(m.median_prev, 0), 4) AS metric_value,
       r.cnt                                               AS rows_examined,
       m.median_prev
  FROM daily r
  LEFT JOIN med m ON m.dt = r.dt
 ORDER BY r.dt DESC
 LIMIT 1;

-- 8. Полнота атрибута segment по активным клиентам (P2).
SELECT ROUND(
           AVG(CASE WHEN segment IS NULL OR btrim(segment) = '' THEN 1.0 ELSE 0.0 END)::numeric,
           4
       ) AS metric_value,
       COUNT(*) AS rows_examined
  FROM mart.dim_client
 WHERE closed_dt IS NULL;

-- 9. Долг выше одобренного лимита - бизнес-инвариант портфеля.
SELECT COUNT(*) AS metric_value,
       (SELECT COUNT(*) FROM mart.fct_loan_portfolio_monthly) AS rows_examined
  FROM mart.fct_loan_portfolio_monthly
 WHERE debt_amt > limit_amt + 0.01;

-- 10. Сверка rowcount: метаданные загрузки против фактического количества строк.
WITH last_batch AS (
    SELECT entity,
           load_dt,
           rows_loaded,
           ROW_NUMBER() OVER (PARTITION BY entity ORDER BY load_dt DESC) AS rn
      FROM ops.etl_batch
     WHERE status = 'SUCCESS'
),
actual AS (
    SELECT 'fct_account_turnover_daily' AS entity, COUNT(*) AS cnt FROM mart.fct_account_turnover_daily
    UNION ALL
    SELECT 'fct_loan_portfolio_monthly', COUNT(*) FROM mart.fct_loan_portfolio_monthly
)
SELECT abs(lb.rows_loaded - a.cnt) AS metric_value,
       a.cnt                       AS rows_examined
  FROM last_batch lb
  JOIN actual a ON a.entity = lb.entity
 WHERE lb.rn = 1
 ORDER BY metric_value DESC
 LIMIT 1;

-- =====================================================================
--  Проверки здоровья самого кластера (задача "мониторинг доступности
--  и производительности инструментов"). Это не DQ данных, а DQ платформы.
-- =====================================================================

-- 11. Перекос данных по сегментам: skew > 1.4 означает, что часть
--     сегментов простаивает, а один является горлышком.
SELECT skcnamespace::text || '.' || skcrelname AS entity,
       skccoeff                                AS metric_value
  FROM gp_toolkit.gp_skew_coefficients
 WHERE skccoeff > 1.4
 ORDER BY skccoeff DESC;

-- 12. Долгие запросы и блокировки - типовой источник "дашборд не грузится".
SELECT pid,
       usename,
       application_name,
       now() - query_start AS duration,
       state,
       wait_event_type,
       left(query, 200) AS query
  FROM pg_stat_activity
 WHERE state <> 'idle'
   AND now() - query_start > interval '10 minutes'
 ORDER BY duration DESC;

-- 13. Мёртвые строки после массовых UPDATE/DELETE в appendonly-таблицах:
--     лечится REORGANIZE, а не VACUUM.
SELECT n.nspname || '.' || c.relname AS entity,
       pg_relation_size(c.oid) / 1024 / 1024 AS mb,
       c.reltuples::bigint AS approx_rows
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'mart'
   AND c.relkind = 'r'
 ORDER BY pg_relation_size(c.oid) DESC;
