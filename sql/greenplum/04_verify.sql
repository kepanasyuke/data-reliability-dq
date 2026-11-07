-- =====================================================================
--  Проверка того, что DDL из 01_mart_ddl.sql действительно лёг так, как
--  задумано: распределение, партиции, сжатие, индексы, ключи и ops-слой.
--
--  Файл ТОЛЬКО ЧИТАЕТ каталог и данные: ни одной DDL/DML-операции.
--  Порядок запуска в pgAdmin Query Tool: выделить блок, Ctrl+Enter.
--  Запускать под суперпользователем (gpadmin): часть запросов смотрит
--  pg_appendonly и gp_distribution_policy.
--
--  Целевая версия: Greenplum 6.x / 7.x. Отличия версий отмечены в
--  комментариях к блокам.
-- =====================================================================


-- ---------------------------------------------------------------------
-- БЛОК 1. Кто мы и на чём стоим.
-- Проверяем, что это действительно MPP-кластер, а не одиночный Postgres
-- (в одиночном Postgres все запросы ниже упадут на gp_segment_configuration).
-- ---------------------------------------------------------------------
SELECT version() AS server_version;

SELECT dbid,
       content,
       role,
       preferred_role,
       status,
       hostname,
       port,
       datadir
  FROM gp_segment_configuration
 ORDER BY content, role;

-- Сколько сегментов реально участвует в работе: если меньше ожидаемого
-- числа, любой skew-анализ ниже будет врать.
SELECT COUNT(*) FILTER (WHERE content >= 0) AS primary_segments
  FROM gp_segment_configuration
 WHERE role = 'p';


-- ---------------------------------------------------------------------
-- БЛОК 2. Распределение таблиц.
-- Ожидаем: dim_client и dim_product - REPLICATED, факты - BY (ключ),
-- ops-таблицы - по своей колонке.
-- ---------------------------------------------------------------------
SELECT n.nspname || '.' || c.relname     AS table_name,
       pg_get_table_distributedby(c.oid) AS distribution
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname IN ('mart', 'ops')
   AND c.relkind IN ('r', 'p')
   AND NOT EXISTS (SELECT 1
                     FROM pg_partitions p
                    WHERE p.schemaname = n.nspname
                      AND p.partitiontablename = c.relname)
 ORDER BY 1;


-- ---------------------------------------------------------------------
-- БЛОК 3. Самопроверка правил Greenplum: PK/UNIQUE обязан содержать
-- все колонки DISTRIBUTED BY.
-- Пустой результат = правила соблюдены во всех таблицах схем mart и ops.
-- Непустой = таблица, которая (по ошибке) вставлена через ALTER или
-- создана до инвентаризации; такую нельзя считать корректной.
-- ---------------------------------------------------------------------
WITH dist AS (
    SELECT p.localoid AS relid,
           string_to_array(btrim(p.distkey::text), ' ')::int2[] AS dk
      FROM gp_distribution_policy p
     WHERE p.distkey IS NOT NULL
       AND btrim(p.distkey::text) <> ''
)
SELECT n.nspname || '.' || c.relname         AS table_name,
       pg_get_constraintdef(con.oid)          AS constraint_def,
       d.dk                                   AS dist_attnums
  FROM dist d
  JOIN pg_class c      ON c.oid = d.relid
  JOIN pg_namespace n  ON n.oid = c.relnamespace
  JOIN pg_constraint con ON con.conrelid = c.oid AND con.contype IN ('p', 'u')
 WHERE n.nspname IN ('mart', 'ops')
   AND NOT (d.dk <@ con.conkey)
 ORDER BY 1;


-- ---------------------------------------------------------------------
-- БЛОК 4. Партиции.
-- Ожидаем 36 месячных партиций + p_default у каждого факта.
-- partitionisdefault = true у p_default - это и есть страховка от
-- "данные за 2028 год потерялись".
-- ---------------------------------------------------------------------
SELECT schemaname,
       tablename,
       partitiontablename,
       partitiontype,
       partitionlevel,
       partitionrangestart,
       partitionrangeend,
       partitionisdefault,
       partitionboundary
  FROM pg_partitions
 WHERE schemaname IN ('mart', 'ops')
 ORDER BY tablename, partitionrank;

-- Сводка: сколько партиций и сколько из них default по каждой таблице.
SELECT schemaname || '.' || tablename                AS table_name,
       COUNT(*)                                      AS partitions,
       COUNT(*) FILTER (WHERE partitionisdefault)    AS default_parts
  FROM pg_partitions
 WHERE schemaname IN ('mart', 'ops')
 GROUP BY 1
 ORDER BY 1;


-- ---------------------------------------------------------------------
-- БЛОК 5. Сжатие и appendonly.
-- Ожидаем relstorage = 'c' (column) и compresstype = 'zstd'.
-- В GP 6.0-6.3 zstd нет: там будет zlib/quicklz/RLE, и это единственное
-- законное расхождение с DDL.
-- ---------------------------------------------------------------------
SELECT n.nspname || '.' || c.relname AS table_name,
       c.relstorage                  AS storage,   -- 'c' column, 'r' row, 'a' heap
       a.blocksize,
       a.compresstype,
       a.compresslevel
  FROM pg_appendonly a
  JOIN pg_class c     ON c.oid = a.relid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname IN ('mart', 'ops')
   AND NOT EXISTS (SELECT 1
                     FROM pg_partitions p
                    WHERE p.schemaname = n.nspname
                      AND p.partitiontablename = c.relname)
 ORDER BY 1;


-- ---------------------------------------------------------------------
-- БЛОК 6. Ключи и индексы.
-- Ожидаем PK у etl_batch, dq_result, dq_incident, dq_catalog.
-- У mart.dim_client PK нет намеренно: Greenplum 6 не разрешает
-- уникальные индексы на appendonly-таблицах.
-- idx_turnover_client и idx_incident_open должны быть на месте.
-- ---------------------------------------------------------------------
SELECT n.nspname || '.' || c.relname AS table_name,
       con.conname                   AS constraint_name,
       con.contype                   AS type,      -- p = PK, u = UNIQUE
       pg_get_constraintdef(con.oid) AS definition
  FROM pg_constraint con
  JOIN pg_class c     ON c.oid = con.conrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname IN ('mart', 'ops')
   AND con.contype IN ('p', 'u')
 ORDER BY 1, 2;

SELECT schemaname, tablename, indexname, indexdef
  FROM pg_indexes
 WHERE schemaname IN ('mart', 'ops')
 ORDER BY 1, 2, 3;


-- ---------------------------------------------------------------------
-- БЛОК 7. Объём и перекос.
-- reltuples - оценка после ANALYZE, а не точный COUNT: на факте точный
-- COUNT дорогой, для инвентаризации оценки достаточно.
-- skccoeff > 1.4 - повод пересмотреть ключ распределения.
-- ---------------------------------------------------------------------
SELECT n.nspname || '.' || c.relname                         AS table_name,
       c.reltuples::bigint                                   AS approx_rows,
       pg_size_pretty(pg_relation_size(c.oid))               AS table_size,
       pg_size_pretty(pg_total_relation_size(c.oid))         AS total_with_indexes
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname IN ('mart', 'ops')
   AND c.relkind IN ('r', 'p')
   AND NOT EXISTS (SELECT 1
                     FROM pg_partitions p
                    WHERE p.schemaname = n.nspname
                      AND p.partitiontablename = c.relname)
 ORDER BY pg_total_relation_size(c.oid) DESC;

SELECT skcnamespace::text || '.' || skcrelname AS table_name,
       skccoeff                                AS skew_coeff
  FROM gp_toolkit.gp_skew_coefficients
 WHERE skccoeff > 1.4
 ORDER BY skccoeff DESC;


-- ---------------------------------------------------------------------
-- БЛОК 8. Проверка, что статистика свежая (иначе планировщик уходит в
-- nested loop - та самая "40 минут вместо 40 секунд").
-- last_analyze пустой при autovacuum выключенном и незапущенном ANALYZE.
-- ---------------------------------------------------------------------
SELECT n.nspname || '.' || c.relname AS table_name,
       c.reltuples::bigint           AS approx_rows,
       s.last_analyze,
       s.last_autoanalyze,
       s.n_live_tup
  FROM pg_class c
  JOIN pg_namespace n   ON n.oid = c.relnamespace
  LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
 WHERE n.nspname IN ('mart', 'ops')
   AND c.relkind IN ('r', 'p')
   AND NOT EXISTS (SELECT 1
                     FROM pg_partitions p
                    WHERE p.schemaname = n.nspname
                      AND p.partitiontablename = c.relname)
 ORDER BY s.last_analyze NULLS FIRST, 1;


-- ---------------------------------------------------------------------
-- БЛОК 9. Partition pruning: то, ради чего нарезали по месяцам.
-- В плане должно быть Partitions scanned = 1..3, а не 36.
-- ---------------------------------------------------------------------
EXPLAIN
SELECT COUNT(*)
  FROM mart.fct_account_turnover_daily
 WHERE dt >= current_date - 90;


-- ---------------------------------------------------------------------
-- БЛОК 10. Ops-слой: журнал загрузок, результаты проверок, инциденты.
-- Блок требует, чтобы уже были выполнены 01_mart_ddl.sql и
-- 02_ops_functions.sql.
-- ---------------------------------------------------------------------
-- 10.1 Свежесть по сущностям: последняя успешная загрузка и её возраст.
SELECT entity,
       MAX(load_dt)                     AS last_success_load,
       ops.dq_age_hours(MAX(load_dt))    AS age_hours,
       COUNT(*)                         AS batches
  FROM ops.etl_batch
 WHERE status = 'SUCCESS'
 GROUP BY entity
 ORDER BY age_hours DESC NULLS LAST;

-- 10.2 Контракт: batch_id обязан быть уникальным по факту загрузки,
-- даже если в PK он составной (batch_id, entity).
SELECT batch_id, COUNT(*) AS rows_in_table, COUNT(DISTINCT entity) AS entities
  FROM ops.etl_batch
 GROUP BY batch_id
HAVING COUNT(*) > 1;

-- 10.3 Результаты последнего прогона проверок.
SELECT run_id, run_ts, status, COUNT(*) AS checks
  FROM ops.dq_result
 GROUP BY run_id, run_ts, status
 ORDER BY run_ts DESC
 LIMIT 10;

-- 10.4 Провалы и ошибки последнего прогона - то, что смотрят на разборе.
SELECT check_id, entity, severity, status, metric_value, message, duration_ms
  FROM ops.dq_result
 WHERE status <> 'PASS'
 ORDER BY run_ts DESC
 LIMIT 20;

-- 10.5 Открытые инциденты по приоритету (индекс idx_incident_open).
SELECT incident_id, severity, status, entity, check_id,
       opened_at, last_seen_at, occurrences, owner
  FROM ops.dq_incident
 WHERE status <> 'RESOLVED'
 ORDER BY CASE severity WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,
          opened_at;

-- 10.6 MTTR по закрытым инцидентам: среднее время до восстановления.
SELECT severity,
       COUNT(*)                                              AS resolved_cnt,
       ROUND(AVG(EXTRACT(EPOCH FROM (resolved_at - opened_at)) / 3600.0)::numeric, 2) AS mttr_hours,
       ROUND(MAX(EXTRACT(EPOCH FROM (resolved_at - opened_at)) / 3600.0)::numeric, 2) AS worst_hours
  FROM ops.dq_incident
 WHERE resolved_at IS NOT NULL
 GROUP BY severity
 ORDER BY 1;

-- 10.7 Инциденты, которые не обновлялись дольше суток, но всё ещё открыты:
-- либо DQ не бежит, либо уведомления не доходят.
SELECT incident_id, severity, status, last_seen_at,
       ops.dq_age_hours(last_seen_at) AS hours_since_last_seen
  FROM ops.dq_incident
 WHERE status <> 'RESOLVED'
   AND ops.dq_age_hours(last_seen_at) > 24
 ORDER BY hours_since_last_seen DESC;

-- 10.8 Проверка идемпотентности dedup_key: один активный инцидент на ключ.
-- Пустой результат = дедупликация работает.
SELECT dedup_key, COUNT(*) AS active_incidents
  FROM ops.dq_incident
 WHERE status <> 'RESOLVED'
 GROUP BY dedup_key
HAVING COUNT(*) > 1;
