# Smoke-test results (программный, через Django Client)

**Дата:** 2026-05-02 20:21
**TOTAL:** 88  **PASS:** 88  **FAIL:** 0

| Раздел | Тест | Статус | Детали |
|---|---|---|---|
| 1.auth | GET /api/v1/health/ (anon) | PASS | HTTP 200 |
| 1.auth | GET / (anon -> 302) | PASS | HTTP 302 |
| 1.auth | GET /login/ | PASS | HTTP 200 |
| 2.pages | GET / | PASS | HTTP 200 |
| 2.pages | GET /list/ | PASS | HTTP 200 |
| 2.pages | GET /list/new/ | PASS | HTTP 200 |
| 2.pages | GET /hawbs/ | PASS | HTTP 200 |
| 2.pages | GET /hawbs/new/ | PASS | HTTP 200 |
| 2.pages | GET /goods/ | PASS | HTTP 200 |
| 2.pages | GET /workflows/ | PASS | HTTP 200 |
| 2.pages | GET /sla/ | PASS | HTTP 200 |
| 2.pages | GET /labels/ | PASS | HTTP 200 |
| 2.pages | GET /faq/ | PASS | HTTP 200 |
| 2.pages | GET /export/ | PASS | HTTP 200 |
| 2.pages | GET /drill/ | PASS | HTTP 200 |
| 2.pages | GET /admin/ | PASS | HTTP 200 |
| 3.api_get | GET /api/v1/cargo/ | PASS | HTTP 200 |
| 3.api_get | GET /api/v1/hawbs/ | PASS | HTTP 200 |
| 3.api_get | GET /api/v1/dashboard/widgets/ | PASS | HTTP 200 |
| 3.api_get | GET /api/v1/dashboard/cql/fields/?entity_type=cargo | PASS | HTTP 200 |
| 3.api_get | GET /api/v1/dashboard/cql/fields/?entity_type=hawb | PASS | HTTP 200 |
| 3.api_get | GET /api/v1/dashboard/pivot/fields/?entity_type=cargo | PASS | HTTP 200 |
| 3.api_get | GET /api/v1/dashboard/pivot/fields/?entity_type=hawb | PASS | HTTP 200 |
| 3.api_get | GET /api/v1/widgets/entities/ | PASS | HTTP 200 |
| 3.api_get | GET /api/v1/widgets/fields/?entity_type=cargo | PASS | HTTP 200 |
| 3.api_get | GET /api/v1/workflows/ | PASS | HTTP 200 |
| 3.api_get | GET /api/v1/sla-policies/ | PASS | HTTP 200 |
| 3.api_get | GET /api/v1/labels/ | PASS | HTTP 200 |
| 3.api_get | GET /api/v1/labels/?with_usage=1 | PASS | HTTP 200 |
| 4.cargo | create cargo 999-99000001 | PASS |  |
| 4.cargo | GET /cargo/999-99000001/ | PASS | HTTP 200 |
| 4.cargo | GET /api/v1/cargo/<awb>/ | PASS | HTTP 200 |
| 4.cargo | GET update (POST-only) | PASS | HTTP 302 |
| 4.cargo | GET assign (POST-only) | PASS | HTTP 302 |
| 4.cargo | GET stage (POST-only) | PASS | HTTP 302 |
| 4.cargo | GET hawb list | PASS | HTTP 200 |
| 4.cargo | GET hawb create (POST-only) | PASS | HTTP 302 |
| 4.cargo | set_stage(FORMED) применился | PASS |  |
| 5.hawb | create HAWB SMOKE-001 (linked) | PASS |  |
| 5.hawb | GET /hawb/48240/ | PASS | HTTP 200 |
| 5.hawb | GET /hawb/48240/update/ (POST-only) | PASS | HTTP 302 |
| 5.hawb | standalone HAWB has no mawb | PASS |  |
| 5.hawb | change_logistics_status | PASS |  |
| 6.labels | POST create | PASS | HTTP 201 |
| 6.labels | PUT update color | PASS | HTTP 200 |
| 6.labels | PUT cargo labels | PASS | HTTP 200 |
| 6.labels | PUT hawb labels | PASS | HTTP 200 |
| 6.labels | with_usage показывает usage_count >= 2 | PASS |  |
| 6.labels | pagination meta | PASS |  |
| 7.widgets | POST create | PASS | HTTP 201 |
| 7.widgets | GET detail | PASS | HTTP 200 |
| 7.widgets | GET data | PASS | HTTP 200 |
| 7.widgets | PUT update | PASS | HTTP 200 |
| 7.widgets | pagination key | PASS |  |
| 7.widgets | pivot fields has groupable | PASS |  |
| 7.widgets | POST layout | PASS | HTTP 200 |
| 8.wf | POST create | PASS | HTTP 201 |
| 8.wf | GET detail | PASS | HTTP 200 |
| 8.wf | GET /workflows/22/ | PASS | HTTP 200 |
| 8.wf | POST step1 | PASS | HTTP 201 |
| 8.wf | POST step2 | PASS | HTTP 201 |
| 8.wf | POST transition | PASS | HTTP 201 |
| 8.wf | POST automation | PASS | HTTP 201 |
| 8.wf | POST layout | PASS | HTTP 200 |
| 8.wf | POST manual start | PASS | HTTP 201 |
| 8.wf | GET instances for cargo | PASS | HTTP 200 |
| 8.wf | POST instance cancel | PASS | HTTP 200 |
| 8.wf | pagination key | PASS |  |
| 8.wf | bad JSON -> 400 | PASS |  |
| 9.sla | POST create | PASS | HTTP 201 |
| 9.sla | PUT update | PASS | HTTP 200 |
| 9.sla | pagination key | PASS |  |
| 9.sla | filter by entity_type | PASS |  |
| 10.cql | validate `stage = FORMED` | PASS |  |
| 10.cql | validate `stage IN (DRAFT, ARRIVED)` | PASS |  |
| 10.cql | validate `weight > 100 AND weight < 500` | PASS |  |
| 10.cql | validate `flight_date BETWEEN -7d AND today()` | PASS |  |
| 10.cql | validate `labels IS NULL` | PASS |  |
| 10.cql | parse-tree -> AST | PASS |  |
| 10.cql | invalid CQL valid=False + error | PASS |  |
| 10.cql | bad JSON -> 400 (B.1) | PASS |  |
| 11.edge | 404 на несуществующий cargo | PASS |  |
| 11.edge | 404 на несуществующий HAWB | PASS |  |
| 11.edge | 404/403 на несуществующий widget | PASS |  |
| 11.edge | 404 на несуществующий workflow | PASS |  |
| 11.edge | pivot fields с невалидным entity_type | PASS |  |
| 11.edge | pagination за пределами -> пустой | PASS |  |
| 11.edge | pagination с буквами -> 200 (fallback) | PASS |  |