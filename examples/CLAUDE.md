---
type: canonical
source: none
sync: none
sla: none
---

# Project: ExampleApp

## Architecture

<!-- drift:intent -->
- Auth module should not import from payment domain
- All database access must go through the repository layer
- Never use console.log in src/production
- API controllers should not contain business logic
- Error responses must always include a correlation ID
<!-- /drift:intent -->

## Decisions

- We chose PostgreSQL over MongoDB because we need ACID transactions
- JWT tokens over session cookies for microservice auth
- TypeScript strict mode is mandatory
