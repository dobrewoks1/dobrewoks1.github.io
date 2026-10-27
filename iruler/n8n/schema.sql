-- Таблицы под сценарий «Сверка транзакций парка» (PostgreSQL 14+).

CREATE TABLE IF NOT EXISTS fleet_txn (
  txn_id     text PRIMARY KEY,            -- id транзакции из Fleet API: ключ идемпотентности
  driver_id  text NOT NULL,
  event_at   timestamptz NOT NULL,
  category   text NOT NULL,
  amount     numeric(12,2) NOT NULL,
  for_date   date NOT NULL,               -- за какой день грузили
  loaded_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fleet_txn_for_date ON fleet_txn (for_date);

-- Строки, которые не прошли проверку: не теряются молча, их видно и можно догрузить.
CREATE TABLE IF NOT EXISTS fleet_txn_rejected (
  id        bigserial PRIMARY KEY,
  run_id    text NOT NULL,
  for_date  date NOT NULL,
  txn_id    text,
  reason    text NOT NULL,
  raw       jsonb NOT NULL,
  seen_at   timestamptz NOT NULL DEFAULT now()
);

-- Журнал запусков: что, когда, сколько строк, чем закончилось.
CREATE TABLE IF NOT EXISTS etl_runs (
  run_id      text PRIMARY KEY,           -- = id выполнения n8n
  for_date    date NOT NULL,
  status      text NOT NULL CHECK (status IN ('running', 'ok', 'error')),
  started_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  fetched     int,
  inserted    int,
  updated     int,
  rejected    int,
  error       text
);
