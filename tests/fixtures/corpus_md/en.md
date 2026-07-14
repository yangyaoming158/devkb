# Order Service

The order service owns the order lifecycle: creation, payment, fulfilment and cancellation.

## API Conventions

All endpoints are versioned under `/api/v1`. Errors follow RFC 7807 problem details.
Idempotency is enforced via the `Idempotency-Key` header on all mutating requests.

## Retry Policy

Downstream calls use exponential backoff with a maximum of three attempts.
Timeouts are set to 2 seconds for reads and 5 seconds for writes.
