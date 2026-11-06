-- Демо-схема локального хранилища (SQLite).
-- Структура повторяет mart-слой Greenplum из sql/greenplum/01_mart_ddl.sql,
-- чтобы одни и те же DQ-проверки работали и локально, и в кластере.

DROP TABLE IF EXISTS fct_account_turnover_daily;
DROP TABLE IF EXISTS fct_loan_portfolio_monthly;
DROP TABLE IF EXISTS etl_batch;
DROP TABLE IF EXISTS dim_client;
DROP TABLE IF EXISTS dim_product;

CREATE TABLE dim_client (
    client_id   TEXT PRIMARY KEY,
    inn         TEXT NOT NULL,
    ogrn        TEXT NOT NULL,
    client_name TEXT NOT NULL,
    segment     TEXT,
    manager_id  TEXT,
    opened_dt   TEXT,
    closed_dt   TEXT
);

CREATE TABLE dim_product (
    product_code TEXT PRIMARY KEY,
    product_name TEXT NOT NULL,
    product_family TEXT NOT NULL,
    is_credit    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE fct_account_turnover_daily (
    account_id      TEXT NOT NULL,
    dt              TEXT NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'RUB',
    client_id       TEXT NOT NULL,
    debit_turnover  REAL NOT NULL DEFAULT 0,
    credit_turnover REAL NOT NULL DEFAULT 0,
    balance_eod     REAL NOT NULL DEFAULT 0,
    src_system      TEXT,
    load_dt         TEXT NOT NULL
);

CREATE TABLE fct_loan_portfolio_monthly (
    client_id    TEXT NOT NULL,
    product_code TEXT NOT NULL,
    month        TEXT NOT NULL,
    limit_amt    REAL NOT NULL DEFAULT 0,
    debt_amt     REAL NOT NULL DEFAULT 0,
    overdue_amt  REAL NOT NULL DEFAULT 0,
    interest_rate REAL,
    load_dt      TEXT NOT NULL
);

CREATE TABLE etl_batch (
    batch_id    TEXT PRIMARY KEY,
    dag_id      TEXT NOT NULL,
    entity      TEXT NOT NULL,
    load_dt     TEXT NOT NULL,
    rows_loaded INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'SUCCESS'
);

CREATE INDEX idx_turnover_dt ON fct_account_turnover_daily (dt);
CREATE INDEX idx_turnover_account ON fct_account_turnover_daily (account_id, dt);
CREATE INDEX idx_turnover_client ON fct_account_turnover_daily (client_id);
CREATE INDEX idx_loan_month ON fct_loan_portfolio_monthly (month);
CREATE INDEX idx_batch_entity ON etl_batch (entity, load_dt);
