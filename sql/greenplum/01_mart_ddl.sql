-- =====================================================================
--  Аналитическое хранилище по ключевым клиентам банка (юрлица).
--  Целевая СУБД: Greenplum 6/7 (PostgreSQL 9.4/12 based MPP).
--  Файл показывает, как устроен mart-слой и какие решения по
--  распределению/партиционированию принимались - это первое, что
--  проверяют на ревью в MPP.
--
--  Правила Greenplum, которые здесь соблюдены (иначе DDL падает):
--   1) все колонки DISTRIBUTED BY обязаны входить в PRIMARY KEY/UNIQUE
--      (распределённая таблица не может гарантировать глобальную
--      уникальность по колонке, которой нет в ключе распределения);
--   2) ключом RANGE-партиции может быть date/timestamp/число, но не
--      varchar - поэтому месяц портфеля хранится как date (первое число),
--      а не как строка 'YYYY-MM' из демо-схемы на SQLite;
--   3) compresstype = zstd требует Greenplum 6.4+ (в 6.0-6.3 - только
--      zlib/quicklz/RLE).
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS mart;
CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS stage;

-- ---------------------------------------------------------------------
-- Справочник клиентов.
-- Клиентов ~ сотни тысяч, join к факту идёт постоянно, поэтому
-- DISTRIBUTED REPLICATED: каждая сегмента не делает redistribute,
-- а broadcast-джойн к маленькому справочнику всегда дешевле.
-- ---------------------------------------------------------------------
CREATE TABLE mart.dim_client (
    client_id       varchar(20)  NOT NULL,
    inn             varchar(12)  NOT NULL,
    ogrn            varchar(15)  NOT NULL,
    client_name     varchar(255) NOT NULL,
    segment         varchar(64),
    manager_id      varchar(20),
    opened_dt       date,
    closed_dt       date,
    load_dt         timestamp NOT NULL DEFAULT now(),
    CONSTRAINT dim_client_pk PRIMARY KEY (client_id)
)
WITH (appendonly = true, orientation = column, compresstype = zstd, compresslevel = 3)
DISTRIBUTED REPLICATED;

COMMENT ON TABLE mart.dim_client IS 'Справочник клиентов юрлиц; реплицирован на все сегменты';
COMMENT ON COLUMN mart.dim_client.inn IS 'ИНН, 10 знаков (ЮЛ) или 12 (ИП), контрольное число проверяется в DQ';

CREATE TABLE mart.dim_product (
    product_code   varchar(32) NOT NULL,
    product_name   varchar(255) NOT NULL,
    product_family varchar(64)  NOT NULL,
    is_credit      boolean      NOT NULL DEFAULT false
)
WITH (appendonly = true, orientation = column, compresstype = zstd)
DISTRIBUTED REPLICATED;

-- ---------------------------------------------------------------------
-- Факт ежедневных оборотов по счетам.
-- DISTRIBUTED BY (account_id): основной паттерн доступа - "по счёту
-- за период" и оконные функции LAG по счёту. Тогда окно считается
-- локально на сегменте без redistribute motion.
-- PARTITION BY RANGE (dt): последние 90 дней в отдельной партиции -
-- запросы дашбордов читают только её (partition pruning), старые
-- партиции можно сжимать и выгружать в архив.
-- ---------------------------------------------------------------------
CREATE TABLE mart.fct_account_turnover_daily (
    account_id      varchar(20)   NOT NULL,
    dt              date          NOT NULL,
    currency        varchar(3)    NOT NULL DEFAULT 'RUB',
    client_id       varchar(20)   NOT NULL,
    debit_turnover  numeric(20,2) NOT NULL DEFAULT 0,
    credit_turnover numeric(20,2) NOT NULL DEFAULT 0,
    balance_eod     numeric(20,2) NOT NULL DEFAULT 0,
    src_system      varchar(32),
    load_dt         timestamp     NOT NULL DEFAULT now()
)
WITH (appendonly = true, orientation = column, compresstype = zstd, compresslevel = 4)
DISTRIBUTED BY (account_id)
PARTITION BY RANGE (dt)
(
    START (DATE '2024-01-01') INCLUSIVE
    END (DATE '2027-01-01') EXCLUSIVE
    EVERY (INTERVAL '1 month'),
    DEFAULT PARTITION p_default
);

COMMENT ON TABLE mart.fct_account_turnover_daily IS
    'Ежедневные обороты и исходящий остаток по расчётным счетам; grain = account_id + dt + currency';

CREATE INDEX idx_turnover_client ON mart.fct_account_turnover_daily (client_id);

-- ---------------------------------------------------------------------
-- Месячный портфель кредитных продуктов.
-- Grain: client_id + product_code + month. Распределение по client_id -
-- отчёты агрегируют портфель по клиенту, а не по продукту.
-- month - тип date (первое число месяца): ключ RANGE-партиции в
-- Greenplum должен быть date/timestamp/числом. В демо на SQLite это
-- строка 'YYYY-MM'; при выгрузке из демо в GP поле конвертируется
-- (month || '-01')::date, обратно - to_char(month, 'YYYY-MM').
-- ---------------------------------------------------------------------
CREATE TABLE mart.fct_loan_portfolio_monthly (
    client_id     varchar(20)   NOT NULL,
    product_code  varchar(32)   NOT NULL,
    month         date          NOT NULL,
    limit_amt     numeric(20,2) NOT NULL DEFAULT 0,
    debt_amt      numeric(20,2) NOT NULL DEFAULT 0,
    overdue_amt   numeric(20,2) NOT NULL DEFAULT 0,
    interest_rate numeric(6,2),
    load_dt       timestamp     NOT NULL DEFAULT now()
)
WITH (appendonly = true, orientation = column, compresstype = zstd)
DISTRIBUTED BY (client_id)
PARTITION BY RANGE (month)
(
    START (DATE '2024-01-01') INCLUSIVE
    END (DATE '2027-01-01') EXCLUSIVE
    EVERY (INTERVAL '1 month'),
    DEFAULT PARTITION p_default
);

-- ---------------------------------------------------------------------
-- Метаданные загрузок: единственный источник правды о том, что и когда
-- приехало. Проверки свежести и сверки rowcount смотрят сюда, а не в
-- system catalog - так они одинаково работают для Greenplum и ClickHouse.
-- PK составной (batch_id, entity): entity - ключ распределения, а
-- Greenplum требует, чтобы все колонки DISTRIBUTED BY входили в PK.
-- Глобальная уникальность batch_id обеспечивается загрузчиком
-- (batch_id = entity + run_ts), отдельный UNIQUE здесь невозможен:
-- он тоже обязан содержать entity.
-- ---------------------------------------------------------------------
CREATE TABLE ops.etl_batch (
    batch_id    varchar(40) NOT NULL,
    dag_id      varchar(120) NOT NULL,
    entity      varchar(120) NOT NULL,
    load_dt     timestamp   NOT NULL,
    rows_loaded bigint      NOT NULL DEFAULT 0,
    status      varchar(20) NOT NULL DEFAULT 'SUCCESS',
    CONSTRAINT etl_batch_pk PRIMARY KEY (batch_id, entity)
)
DISTRIBUTED BY (entity);

-- ---------------------------------------------------------------------
-- Результаты проверок и инциденты (тот же контракт, что в reliability/store.py).
-- ---------------------------------------------------------------------
CREATE TABLE ops.dq_result (
    run_id        varchar(40) NOT NULL,
    run_ts        timestamp   NOT NULL,
    check_id      varchar(120) NOT NULL,
    entity        varchar(120) NOT NULL,
    severity      varchar(4)  NOT NULL,
    status        varchar(8)  NOT NULL,
    metric_value  numeric(24,6),
    unit          varchar(16),
    message       text,
    rows_examined bigint,
    duration_ms   integer,
    samples_json  text,
    CONSTRAINT dq_result_pk PRIMARY KEY (run_id, check_id)
)
DISTRIBUTED BY (check_id);

-- Инциденты. Распределены по dedup_key: и открытие, и продление, и
-- автозакрытие идут предикатом WHERE dedup_key = ..., поэтому строка
-- истории инцидента всегда лежит на одном сегменте.
-- PK составной (incident_id, dedup_key) - по тому же правилу Greenplum,
-- что и у ops.etl_batch: ключ распределения обязан быть в PK.
-- ---------------------------------------------------------------------
CREATE TABLE ops.dq_incident (
    incident_id     varchar(20) NOT NULL,
    dedup_key       varchar(200) NOT NULL,
    check_id        varchar(120) NOT NULL,
    entity          varchar(120) NOT NULL,
    severity        varchar(4)  NOT NULL,
    status          varchar(12) NOT NULL,
    title           varchar(255) NOT NULL,
    owner           varchar(120),
    opened_at       timestamp   NOT NULL,
    acked_at        timestamp,
    resolved_at     timestamp,
    last_seen_at    timestamp   NOT NULL,
    occurrences     integer     NOT NULL DEFAULT 1,
    latest_run_id   varchar(40),
    metric_value    numeric(24,6),
    bounds_json     text,
    samples_json    text,
    resolution_note text,
    CONSTRAINT dq_incident_pk PRIMARY KEY (incident_id, dedup_key)
)
DISTRIBUTED BY (dedup_key);

CREATE INDEX idx_incident_open ON ops.dq_incident (status, severity);

-- ---------------------------------------------------------------------
-- Регламентные операции:
--   ANALYZE после каждой загрузки (иначе планировщик выбирает nested loop
--   и запрос "встаёт" на 40 минутах вместо 40 секунд);
--   VACUUM на appendonly-таблицах не нужен - вместо него REORGANIZE
--   при массовых UPDATE/DELETE.
-- ---------------------------------------------------------------------
-- ANALYZE mart.fct_account_turnover_daily;
-- VACUUM mart.fct_account_turnover_daily;  -- heap tables only
