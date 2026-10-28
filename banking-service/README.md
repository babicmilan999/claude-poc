# banking-service

Mock core banking for the payments support POC (Java 21, Spring Boot 3, Flyway, schema `banking`).
API: `contracts/banking-api.yaml`. Design: `docs/architecture.md` sections 3, 5, 6 and 9.

```
./mvnw test                                   # or: make test-banking (H2, no Docker)
SPRING_PROFILES_ACTIVE=simulation ./mvnw spring-boot:run   # needs Postgres (SPRING_DATASOURCE_*)
```

## Layout (`src/main/java/com/claudepoc/banking`)

| Package | What |
|---|---|
| `domain` | Entities, enums, repositories |
| `rules` | The decision rules: card authorisation, fraud (FR-12, FR-07), payments, direct debit collections |
| `operations` | Domain operations (freeze a card, authorise a payment, run a standing order, process a collection...); the only code that changes data |
| `api` | Read-only REST API, problem details for errors (404 for ids not owned by the customer) |
| `situations` | Customer situations built only from domain operations; each checks the rules gave the intended reason code |
| `seed` | 13 seeded customers on an empty database (fixed ids `00000000-0000-4000-8000-0000000000NN`) |
| `simulation` | `POST /simulation/incidents`, only with the Spring profile `simulation`; holds the answer key |

## Seeded customers

| # | Name | Situation |
|---|---|---|
| 1 | Alice Morgan | no problems (standing order, direct debit, card payments all fine) |
| 2 | Ben Carter | card frozen by the customer, two `CARD_FROZEN` declines today |
| 3 | Chloe Davies | rent standing order failed `INSUFFICIENT_FUNDS` (and its retry) |
| 4 | Daniel Evans | direct debit failed `MANDATE_CANCELLED` (customer cancelled the mandate) |
| 5 | Emily Foster | `FRAUD_RULE_TRIGGERED` (FR-12), card blocked, open fraud flag, then `CARD_BLOCKED` |
| 6 | Farah Ghani | old card expired, `CARD_EXPIRED` |
| 7 | George Harris | vulnerable customer, `INSUFFICIENT_FUNDS` card decline |
| 8 | Hannah Ibrahim | standing order failed `PAYEE_ACCOUNT_CLOSED` |
| 9 | Isaac Jones | `MERCHANT_CATEGORY_BLOCKED` (gambling blocked by the customer) |
| 10 | Julia King | direct debit failed `ACCOUNT_CLOSED_BY_ORIGINATOR` |
| 11 | Kofi Mensah | `CONTACTLESS_LIMIT_EXCEEDED` (customer lowered the limit) |
| 12 | Laura Nowak | direct debit failed `INSUFFICIENT_FUNDS` |
| 13 | Mark Owen | standing order failed `DAILY_PAYMENT_LIMIT_EXCEEDED` |

## Simulation params

`params` in `POST /simulation/incidents` (all optional): `merchant_name`, `merchant_mcc`, `amount`,
`declined_attempts` (card scenarios), `contactless_limit`, `payee_name`, `daily_limit`, `originator`.

## Settings

Standard Spring variables: `SPRING_DATASOURCE_URL`, `SPRING_DATASOURCE_USERNAME`, `SPRING_DATASOURCE_PASSWORD`,
`SPRING_PROFILES_ACTIVE` (`simulation` enables the simulation endpoint), `SERVER_PORT` (default 8080).
`BANKING_SEED_ENABLED` (default `true`) seeds the customers when the database is empty.
