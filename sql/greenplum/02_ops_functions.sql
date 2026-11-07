-- =====================================================================
--  Серверная часть сопровождения: функции и процедуры, которые
--  переиспользуют и DQ-проверки, и дашборды, и Airflow-таски.
--  Всё в PL/pgSQL (Greenplum 6+), без внешних расширений.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS ops;

-- ---------------------------------------------------------------------
-- Возраст загрузки в часах. Одна функция вместо копирования
-- EXTRACT(EPOCH FROM now() - load_dt)/3600 по десятку проверок.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ops.dq_age_hours(p_ts timestamp)
RETURNS numeric
LANGUAGE sql
STABLE
AS $$
    SELECT ROUND(COALESCE(EXTRACT(EPOCH FROM (now() - p_ts)) / 3600.0, 0)::numeric, 3);
$$;

COMMENT ON FUNCTION ops.dq_age_hours(timestamp) IS
    'Возраст метки времени в часах; NULL трактуется как 0 (нет загрузки - отдельная проверка полноты)';

-- ---------------------------------------------------------------------
-- Следующий номер инцидента. Последовательность в MPP не гарантирует
-- монотонность между сегментами, поэтому номер берём из ops-таблицы,
-- а не из sequence - иначе в тикетах появляются "дырки".
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ops.dq_next_incident_id()
RETURNS varchar
LANGUAGE plpgsql
AS $$
DECLARE
    v_next integer;
BEGIN
    SELECT COALESCE(MAX(CAST(SUBSTR(incident_id, 4) AS integer)), 0) + 1
      INTO v_next
      FROM ops.dq_incident;

    RETURN 'DQ-' || LPAD(v_next::text, 5, '0');
END;
$$;

-- ---------------------------------------------------------------------
-- Открыть инцидент или продлить существующий (дедупликация по
-- check_id || entity). Ровно та же логика, что в
-- reliability/incidents.py::sync_incidents - реализована и в SQL,
-- чтобы загрузка могла сама регистрировать инцидент, если Airflow лежит.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ops.dq_register_failure(
    p_check_id   varchar,
    p_entity     varchar,
    p_severity   varchar,
    p_title      varchar,
    p_metric     numeric,
    p_run_id     varchar,
    p_owner      varchar DEFAULT 'data-reliability@bank.local',
    p_samples    text DEFAULT NULL
)
RETURNS varchar
LANGUAGE plpgsql
AS $$
DECLARE
    v_key      varchar(200);
    v_existing ops.dq_incident%ROWTYPE;
    v_id       varchar(20);
BEGIN
    v_key := p_check_id || '|' || p_entity;

    SELECT * INTO v_existing
      FROM ops.dq_incident
     WHERE dedup_key = v_key
       AND status <> 'RESOLVED'
     ORDER BY opened_at DESC
     LIMIT 1;

    IF FOUND THEN
        UPDATE ops.dq_incident
           SET last_seen_at = now(),
               occurrences  = occurrences + 1,
               metric_value = p_metric,
               latest_run_id = p_run_id,
               samples_json = COALESCE(p_samples, samples_json)
         WHERE incident_id = v_existing.incident_id
           AND dedup_key   = v_key;

        RETURN v_existing.incident_id;
    END IF;

    v_id := ops.dq_next_incident_id();

    INSERT INTO ops.dq_incident
        (incident_id, dedup_key, check_id, entity, severity, status, title,
         owner, opened_at, last_seen_at, occurrences, latest_run_id, metric_value, samples_json)
    VALUES
        (v_id, v_key, p_check_id, p_entity, p_severity, 'OPEN', p_title,
         p_owner, now(), now(), 1, p_run_id, p_metric, p_samples);

    RETURN v_id;
END;
$$;

COMMENT ON FUNCTION ops.dq_register_failure(varchar, varchar, varchar, varchar, numeric, varchar, varchar, text) IS
    'Идемпотентная регистрация инцидента: повторный вызов по тому же check_id+entity увеличивает occurrences';

-- ---------------------------------------------------------------------
-- Автозакрытие инцидента при успешной проверке.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ops.dq_register_success(p_check_id varchar, p_entity varchar, p_run_id varchar)
RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
    v_count integer;
BEGIN
    UPDATE ops.dq_incident
       SET status = 'RESOLVED',
           resolved_at = now(),
           last_seen_at = now(),
           latest_run_id = p_run_id,
           resolution_note = 'auto-resolved: check passed in run ' || p_run_id
     WHERE dedup_key = p_check_id || '|' || p_entity
       AND status <> 'RESOLVED';

    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$;

-- ---------------------------------------------------------------------
-- Проверка свежести: одна строка на сущность, годится для дашборда
-- и для HealthCheck-таски Airflow.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ops.dq_freshness(p_entity varchar, p_sla_hours numeric)
RETURNS TABLE (
    entity       varchar,
    last_load_dt timestamp,
    age_hours    numeric,
    sla_hours    numeric,
    status       text
)
LANGUAGE sql
STABLE
AS $$
    SELECT p_entity,
           b.last_load,
           ops.dq_age_hours(b.last_load),
           p_sla_hours,
           CASE
               WHEN b.last_load IS NULL THEN 'FAIL'
               WHEN ops.dq_age_hours(b.last_load) > p_sla_hours THEN 'FAIL'
               WHEN ops.dq_age_hours(b.last_load) > p_sla_hours * 0.8 THEN 'WARN'
               ELSE 'PASS'
           END
      FROM (
            SELECT MAX(load_dt) AS last_load
              FROM ops.etl_batch
             WHERE entity = p_entity
               AND status = 'SUCCESS'
           ) b;
$$;

-- ---------------------------------------------------------------------
-- Процедура "прогнать каталог проверок" для случая, когда DQ нужно
-- вызвать извне (Airflow BashOperator -> psql -c "...").
-- Реестр проверок читается из ops.dq_catalog, чтобы не тащить
-- Python туда, где его нет.
--
-- Важно про версии: в Greenplum 6 (PostgreSQL 9.4) конструкции
-- CREATE PROCEDURE нет, поэтому здесь функция RETURNS void и вызов:
--     SELECT ops.dq_run_all('run-2026-11-07');
-- В Greenplum 7 этот же код можно объявить как PROCEDURE и вызывать
-- через CALL ops.dq_run_all('run-...').
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ops.dq_catalog (
    check_id   varchar(120) PRIMARY KEY,
    entity     varchar(120) NOT NULL,
    severity   varchar(4)   NOT NULL,
    title      varchar(255) NOT NULL,
    metric_sql text         NOT NULL,
    fail_max   numeric,
    fail_min   numeric,
    is_active  boolean      NOT NULL DEFAULT true
)
DISTRIBUTED REPLICATED;

CREATE OR REPLACE FUNCTION ops.dq_run_all(p_run_id varchar, p_owner varchar DEFAULT 'data-reliability@bank.local')
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    r           record;
    v_metric    numeric;
    v_status    varchar(8);
    v_started   timestamptz;
BEGIN
    FOR r IN SELECT * FROM ops.dq_catalog WHERE is_active LOOP
        v_started := clock_timestamp();
        v_metric  := NULL;
        v_status  := 'PASS';

        BEGIN
            EXECUTE r.metric_sql INTO v_metric;
        EXCEPTION WHEN OTHERS THEN
            v_status := 'ERROR';
            INSERT INTO ops.dq_result
                (run_id, run_ts, check_id, entity, severity, status, message, duration_ms)
            VALUES
                (p_run_id, now(), r.check_id, r.entity, r.severity, 'ERROR', SQLERRM,
                 (EXTRACT(EPOCH FROM (clock_timestamp() - v_started)) * 1000)::integer);
            CONTINUE;
        END;

        IF v_metric IS NULL THEN
            v_status := 'FAIL';
        ELSIF r.fail_max IS NOT NULL AND v_metric > r.fail_max THEN
            v_status := 'FAIL';
        ELSIF r.fail_min IS NOT NULL AND v_metric < r.fail_min THEN
            v_status := 'FAIL';
        END IF;

        INSERT INTO ops.dq_result
            (run_id, run_ts, check_id, entity, severity, status, metric_value, message, duration_ms)
        VALUES
            (p_run_id, now(), r.check_id, r.entity, r.severity, v_status, v_metric,
             r.title || ': ' || COALESCE(v_metric::text, 'NULL'),
             (EXTRACT(EPOCH FROM (clock_timestamp() - v_started)) * 1000)::integer);

        IF v_status = 'FAIL' THEN
            PERFORM ops.dq_register_failure(r.check_id, r.entity, r.severity, r.title, v_metric, p_run_id, p_owner);
        ELSE
            PERFORM ops.dq_register_success(r.check_id, r.entity, p_run_id);
        END IF;
    END LOOP;
END;
$$;

COMMENT ON FUNCTION ops.dq_run_all(varchar, varchar) IS
    'Последовательный прогон каталога ops.dq_catalog с записью результатов и синхронизацией инцидентов';
