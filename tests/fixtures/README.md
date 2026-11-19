# Fixtures
Real API responses captured in Postman (Nov 2026). Use them to test adapters offline.

workday/
- cboe_list_page1.json: POST list, limit 20, offset 0 (total 114, facets present)
- cboe_list_offset20.json: page 2 (total and facets unreliable after page 1)
- cboe_list_tech_facet.json: list filtered by jobFamilyGroup facet (total 49)
- cboe_job_detail_R-4331.json: detail call (has startDate)
- gm_list_page1.json: GM list page 1

greenhouse/
- databricks_list.json (892 jobs), stripe_list.json (791), hubspot_list.json (139), imc_list.json (156, EU-hosted board)
- optiver_private_list_content.json: junk board, content=true; leadership-owner names/emails REDACTED
- databricks_job_detail_8472398002.json: detail response (content is HTML-escaped)
- bad_token_404.json: error body for a nonexistent board token

Not included: the HTTP 400 body for Workday limit>20 (empty message).
