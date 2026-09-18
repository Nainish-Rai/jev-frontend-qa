# Scenario authoring

Use `jev-qa schema scenario` as the installed schema authority. A source checkout
is not required. The examples below illustrate declared fictional integration
facts; they are not evidence about the user's app and must not be copied blindly.

## Requirements before implementation details

For each acceptance criterion record the expected outcome, the journey that
exercises it, the observable assertion, and any blocker. Learn selectors and routes
from the repo or approved observations. Do not derive the expected result from the
current implementation: that can turn a bug into its own passing test.

A scenario requires `name`, `start_url`, and nonempty `steps`. Each step requires
`id`, `goal`, and, in contract mode, nonempty `assertions`. Optional `fixtures` map
field names or exact accessible labels to strings. Optional `scope` restricts the
model's action targets, not all assertion selectors. Use a known CSS selector or
`{"testid":"value"}`; assertions outside the action scope still need exact selectors.

Omit `run_id` to obtain a fresh UUID on each load. `{{run_id}}`, exact fixtures,
and verified captures can be interpolated into later expectations and scopes.
Capture names must not overwrite other variables. Use different fixture keys
where a journey needs both original and revised values.

## Choose observable checks

| Requirement | Supported check |
| --- | --- |
| Element text (`textContent`, not a live input value) | `equals` on one selected element |
| Text within one element | `contains` on one selected element |
| Number of matching DOM elements | `count` |
| Attribute value or absence | `attribute` with `name` and string/null `expected` |
| Exact API exchange | `network`: method, path, expected_status; optional payload_contains, response_contains, capture |
| API status only | `status`: method, path, expected_status |
| No request to an endpoint during the step | `no_request`: method, path |
| Independently persisted record or absence | `persistence`: path, records_path, nonempty contains, optional absent |

Network `path` is the exact URL pathname, without query string; current assertions
cannot verify query parameters or headers. Repeated matching exchanges may be
ambiguous: scope the journey so the required exchange is distinguishable. Nested
request/response fields must match within the same object, not across records.

DOM value assertions require exactly one match; use `count` for absence. DOM
checks do not establish visual appearance or pixel-level visibility. Attribute
assertions compare attribute strings, not JS properties (for example a disabled
HTML attribute often has the value `""`).

For a networked mutation, verify its required payload/status/body and UI outcome;
when persistence is required, declare `persistence` against an actual authorized
same-origin GET. It reloads and independently fetches fresh state. `records_path`
locates records such as `$.items`, then `contains` matches all fields in one record.
For read-only/client-only features, use the relevant DOM checks and no-request
checks without inventing backend or persistence requirements. No-request proves
absence only in the completed observation window, not for all future time.

Downloads, uploaded files, query/header assertions, URL transitions, visual
judgment, native selects, frames, and popups require explicit unsupported coverage.
Do not substitute HTTP status for a downloaded file's contents or a redirect URL.

## Example: read-only catalog search

Assumed requirement: entering `orchid` and submitting renders exactly two cards
and a results summary containing `Orchid`. Assumed integration: URL and test IDs
below, an input named `query`, and a Search button. No API/query-parameter or
persistence requirement is being certified by this contract.

```json
{
  "name": "Catalog search",
  "start_url": "http://127.0.0.1:3000/catalog",
  "steps": [{
    "id": "search",
    "goal": "Enter the exact query fixture and click Search once.",
    "scope": {"testid": "catalog-search"},
    "fixtures": {"query": "orchid"},
    "assertions": [
      {"kind": "count", "selector": {"testid": "product-card"}, "expected": 2},
      {"kind": "contains", "selector": {"testid": "results"}, "expected": "Orchid"}
    ]
  }]
}
```

## Example: client-side promotion-code rejection

Assumed requirement: submitting exact code `AB1` displays the stated error and
sends no POST to `/api/promotions`. Assumed integration: URL/test IDs below, an
input named `code`, and an Apply button. This verifies client rejection, not the
API's direct validation behavior or a database invariant.

```json
{
  "name": "Promotion code rejection",
  "start_url": "http://127.0.0.1:3000/promotions",
  "steps": [{
    "id": "reject-short-code",
    "goal": "Enter the exact code fixture and click Apply once.",
    "scope": {"testid": "promotion-form"},
    "fixtures": {"code": "AB1"},
    "assertions": [
      {"kind": "count", "selector": {"testid": "code-error"}, "expected": 1},
      {"kind": "equals", "selector": {"testid": "code-error"}, "expected": "Code must have at least 6 characters"},
      {"kind": "no_request", "method": "POST", "path": "/api/promotions"}
    ]
  }]
}
```

## Ownership and cleanup for mutations

Create synthetic, uniquely marked records through the UI and capture the resulting
identifier from the verified response, for example `capture: {"record_id":"$.id"}`.
Captured IDs currently allow ASCII letters, digits, `_`, and `-`; other ID formats
are unsupported rather than escaped or guessed. Scope subsequent actions to that
identifier. Never operate on whichever row happens to appear first.

Automatic cleanup requires an explicit scope referencing an ID captured from this
run's verified successful POST, with the run marker in its expected request payload.
A PUT-only creation flow or an existing fixture is not automatically cleanup-owned.
Do not claim restoration of unrelated settings or pre-existing records. If safe
setup/cleanup cannot be established, report the missing prerequisite.

`skip_if_absent` requires a scope; it does not erase assertions. Idempotent cleanup
can assert DOM absence and fresh persisted absence rather than require a DELETE
exchange when the record was already removed. Always report cleanup failures
separately. Ambiguous writes require caller review, not speculative cleanup.
