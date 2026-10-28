-- Banking domain tables (architecture section 9). Flyway creates and targets schema `banking`.
-- Plain SQL that runs on PostgreSQL and on H2 in PostgreSQL mode (tests).
-- Enum columns hold the Java enum names (upper case); the API maps them to the contract's values.

create table customer (
    id          uuid primary key,
    name        varchar(200) not null,
    email       varchar(320) not null,
    status      varchar(20)  not null check (status in ('ACTIVE', 'RESTRICTED', 'CLOSED')),
    vulnerable  boolean      not null default false,
    created_at  timestamp with time zone not null
);

create table account (
    id                   uuid primary key,
    customer_id          uuid          not null references customer (id),
    name                 varchar(100)  not null,
    sort_code            varchar(8)    not null,
    account_number       varchar(8)    not null,
    currency             varchar(3)    not null,
    balance              numeric(14,2) not null,
    overdraft_limit      numeric(14,2) not null,
    daily_payment_limit  numeric(14,2) not null,
    status               varchar(20)   not null check (status in ('ACTIVE', 'FROZEN', 'CLOSED')),
    opened_at            timestamp with time zone not null,
    constraint uq_account_number unique (sort_code, account_number)
);
create index ix_account_customer on account (customer_id);

create table card (
    id                          uuid primary key,
    account_id                  uuid          not null references account (id),
    last4                       varchar(4)    not null,
    brand                       varchar(20)   not null check (brand in ('VISA', 'MASTERCARD')),
    status                      varchar(20)   not null check (status in ('ACTIVE', 'FROZEN', 'BLOCKED', 'EXPIRED')),
    status_reason               varchar(30)   check (status_reason in ('FROZEN_BY_CUSTOMER', 'FRAUD_SUSPECTED', 'LOST_OR_STOLEN', 'EXPIRED')),
    expiry                      date          not null,
    contactless_limit           numeric(14,2) not null,
    blocked_merchant_categories varchar(500)  not null default '',
    online_payments_enabled     boolean       not null default true,
    issued_at                   timestamp with time zone not null
);
create index ix_card_account on card (account_id);

create table card_authorisation (
    id             uuid primary key,
    card_id        uuid          not null references card (id),
    amount         numeric(14,2) not null,
    currency       varchar(3)    not null,
    merchant_name  varchar(200)  not null,
    merchant_mcc   varchar(4)    not null,
    channel        varchar(20)   not null check (channel in ('CHIP_AND_PIN', 'CONTACTLESS', 'ONLINE')),
    result         varchar(20)   not null check (result in ('APPROVED', 'DECLINED')),
    decline_reason varchar(40),
    fraud_rule_id  varchar(20),
    created_at     timestamp with time zone not null
);
create index ix_card_authorisation_card on card_authorisation (card_id, created_at);

create table fraud_flag (
    id           uuid primary key,
    customer_id  uuid         not null references customer (id),
    card_id      uuid         references card (id),
    rule_id      varchar(20)  not null,
    description  varchar(500) not null,
    risk_score   numeric(4,3) not null,
    status       varchar(20)  not null check (status in ('OPEN', 'CLEARED', 'CONFIRMED_FRAUD')),
    action       varchar(20)  not null check (action in ('CARD_BLOCKED', 'REVIEW_ONLY')),
    created_at   timestamp with time zone not null,
    cleared_at   timestamp with time zone
);
create index ix_fraud_flag_customer on fraud_flag (customer_id);

create table account_transaction (
    id            uuid primary key,
    account_id    uuid          not null references account (id),
    amount        numeric(14,2) not null,
    currency      varchar(3)    not null,
    type          varchar(30)   not null,
    description   varchar(300)  not null,
    reference_id  uuid,
    booked_at     timestamp with time zone not null
);
create index ix_account_transaction_account on account_transaction (account_id, booked_at);

create table payee (
    id              uuid primary key,
    customer_id     uuid         not null references customer (id),
    name            varchar(200) not null,
    sort_code       varchar(8)   not null,
    account_number  varchar(8)   not null,
    account_status  varchar(20)  not null check (account_status in ('OPEN', 'CLOSED', 'UNKNOWN'))
);
create index ix_payee_customer on payee (customer_id);

create table scheduled_payment (
    id          uuid primary key,
    account_id  uuid          not null references account (id),
    payee_id    uuid          not null references payee (id),
    reference   varchar(100)  not null,
    amount      numeric(14,2) not null,
    currency    varchar(3)    not null,
    frequency   varchar(20)   not null check (frequency in ('ONCE', 'WEEKLY', 'MONTHLY')),
    next_run    date          not null,
    status      varchar(20)   not null check (status in ('ACTIVE', 'PAUSED', 'CANCELLED', 'COMPLETED')),
    created_at  timestamp with time zone not null
);
create index ix_scheduled_payment_account on scheduled_payment (account_id);

create table payment_attempt (
    id                    uuid primary key,
    scheduled_payment_id  uuid          not null references scheduled_payment (id),
    amount                numeric(14,2) not null,
    run_date              date          not null,
    result                varchar(20)   not null check (result in ('SUCCEEDED', 'FAILED', 'SKIPPED')),
    failure_reason        varchar(40),
    retry_count           integer       not null default 0,
    attempted_at          timestamp with time zone not null
);
create index ix_payment_attempt_sp on payment_attempt (scheduled_payment_id, attempted_at);

create table dd_mandate (
    id            uuid primary key,
    account_id    uuid         not null references account (id),
    originator    varchar(200) not null,
    reference     varchar(100) not null,
    status        varchar(20)  not null check (status in ('ACTIVE', 'CANCELLED', 'DORMANT')),
    cancelled_by  varchar(20)  check (cancelled_by in ('CUSTOMER', 'ORIGINATOR', 'BANK')),
    created_at    timestamp with time zone not null,
    cancelled_at  timestamp with time zone
);
create index ix_dd_mandate_account on dd_mandate (account_id);

create table dd_collection (
    id            uuid primary key,
    mandate_id    uuid          not null references dd_mandate (id),
    amount        numeric(14,2) not null,
    currency      varchar(3)    not null,
    due_date      date          not null,
    result        varchar(20)   not null check (result in ('PENDING', 'COLLECTED', 'FAILED')),
    failure_code  varchar(40),
    presented_at  timestamp with time zone not null,
    processed_at  timestamp with time zone
);
create index ix_dd_collection_mandate on dd_collection (mandate_id, due_date);
