# Public search routing

`search_routing.json` beside `official_test_runtime` enables a tool-free DeepSeek Flash
classifier using the existing authenticated provider. The prompt is identical to
the 36-item blind evaluation. `enabled:false` disables automatic pre-search;
explicit public web tools still obey the same policy, limits and evidence checks.

```json
{"enabled":true,"daily_limit_usd":"0.10","monthly_limit_usd":"1.00","max_web_calls":6,"total_web_seconds":40,"priority_reserve_usd":"0.0015"}
```

The monetary limits are a conservative search-specific sub-budget, shared by
classification and the official Search/Fetch APIs. There is no existing monetary
DeepSeek-wide ledger in this application; this does not claim to enforce a
nonexistent shared supplier budget. Reservations include in-flight/unknown
requests. Actual usage including cache hits settles classification; missing tariff
or usage leaves reconcilable liabilities/bounds. Successful nonempty Search and
Fetch each cost $0.00075. Empty/error responses settle zero when confirmed.
Cost discrepancies do not globally pause chat. Insufficient budget or a cold,
busy, failed or >3-second classifier falls back to local rules automatically.

Each turn permits six web operations over 40 seconds. Individual DNS/HTTP workers
have process deadlines and are terminated on expiry. Network prohibitions apply
before classification, automatic prefetch, web tools and video retrieval, including
the fallback lane. Same-author pending question fragments and references carry
instructions; unrelated users' old prohibitions do not.

Search returns relevant source candidates with publication date/unknown status,
requested-month fit and region. Login/captcha/irrelevant/homepage results are not
usable evidence. Snippets are not verified facts. Read uses public HTTPS URL
permission checks for both paid and ordinary requests and offers pagination.
No captcha or access restriction is bypassed. Official international-server
Genshin version announcements are a labelled fallback, never a mainland claim.

Source IDs [W1] are scoped to session and turn. Only committed replies acquire
immutable snapshots, and only acknowledged messages are traceable by a later
quote. QQ escaping is decoded when matching IDs. Unmarked sources remain labelled
turn references, not asserted evidence for every sentence. Durable snapshots retain
1200-character excerpts; full page caches expire after 24 hours. `web.sources`
serves same-group delivered reply evidence locally even when network is forbidden.

Verification artifacts (operator instance, 2026-11-12):
- `/opt/hakimi/search-eval-20261112/final-router/results.json`: final backend,
  35 classifier calls and one cold-start local fallback, 36/36 overall correct;
  classifier actual total $0.0031296. Earlier model-only run missed one positive.
- `/opt/hakimi/search-eval-20261112/final-web-probe/results.json`: real automatic
  official search, actual page Fetch, prompt injection, isolated reply commit,
  local ACK and offline quoted source lookup. No QQ test messages were sent.

Explicit search instructions bypass classification; automatic paid calls leave $0.0015 available for an explicit Search plus Fetch. Low-budget automatic work may use the ordinary bounded public fallback. Fallback prompt pressure omits newly injected page bodies before changing original chat/tool context or increasing any hard limit.
