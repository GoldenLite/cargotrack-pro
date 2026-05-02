# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CargoTrack Pro — Django web app for air freight customs clearance and cargo tracking. Manages Master Air Waybills (MAWB/Cargo) and House Air Waybills (HAWB/HouseWaybill), warehouse operations (СВХ), customs documentation, and cargo staging.

**Stack:** Django 4.2.13, SQLite3, Bootstrap templates, openpyxl for Excel export.
**Language:** UI text in Russian, code identifiers in English.
**Timezone:** Europe/Moscow.

## Development Commands

```bash
pip install -r requirements.txt        # Install deps
python manage.py runserver             # Dev server at http://localhost:8000
python manage.py migrate               # Apply migrations
python manage.py makemigrations        # Generate new migrations
python manage.py createsuperuser       # Create admin user
python manage.py load_test_data --count 50 --hawb-per-mawb 5  # Populate test data
```

No automated test suite exists — all testing is manual.

## Architecture

Single Django app (`cargo`) under the `cargotrack` project. All views are function-based with `@login_required`. No REST API — purely server-rendered templates with POST handlers for state changes.

### Key Models (cargo/models.py)

- **Cargo** — MAWB (Master Air Waybill). The top-level shipment batch.
- **HouseWaybill** — HAWB. Individual item within a MAWB, or standalone (mawb FK is nullable).
- **HAWBGood** — Line item (товарная позиция) inside a HAWB.
- **HAWBDocument** — Document attachment to a HAWB.
- **Warehouse** — СВХ (temporary storage facility) reference.
- **Flight** — Flight reference data.
- **StatusHistory** — Audit log for Cargo status changes.
- **CargoAssignment** — M2M: User ↔ Cargo with role (declarant/broker/manager/supervisor/inspector), unique_together on (cargo, user, role).

### Three Independent Status Systems

This is the most important architectural concept:

1. **Cargo.stage** (6 states): `DRAFT → FORMED → DISPATCHED → ARRIVED → CUSTOMS → RELEASED`. Primary workflow axis. Controls Kanban board columns on the dashboard.
2. **HouseWaybill.logistics_status** (20 states): Full item lifecycle from `CREATED` through delivery or error. The active status system for HAWBs.
3. **HouseWaybill.customs_status** (8 states): Sub-status active only when logistics_status is `EXPORT_CUSTOMS` or `IMPORT_CUSTOMS`. Tracks customs processing (BROKER_CHECK → RELEASED).

**Legacy fields** (`Cargo.status`, `Cargo.queue`, `HouseWaybill.status`): Retained for backwards compatibility. `queue` is auto-synced with `stage` in `Cargo.save()`. The legacy `STATUS_CHOICES` is an empty list.

### Auto-Workflow Rules

- **Cargo**: `check_auto_stage()` promotes `CUSTOMS → RELEASED` when total released HAWB weight matches cargo weight.
- **Cargo**: `set_stage('ARRIVED')` auto-sets `scan_into_bond` timestamp.
- **HAWB**: Entering `EXPORT_CUSTOMS`/`IMPORT_CUSTOMS` auto-sets `customs_status = 'BROKER_CHECK'`.
- **HAWB**: `customs_status → RELEASED` auto-advances logistics: `IMPORT_CUSTOMS → READY_DELIVERY`, `EXPORT_CUSTOMS → IN_TRANSIT_EXP`.
- **HAWB save()**: Auto-clears `customs_declaration_number` if docs checklist is incomplete or mawb is null.
- **HAWB save()**: Auto-clears `scan_into_bond` if no mawb or cargo is in transit.

### Document Readiness (HAWB)

Four boolean checkbox fields: `doc_invoice`, `doc_packing_list`, `doc_permit`, `doc_tech_desc`. The `docs_required` field (default 4) controls how many must be checked before a customs declaration number can be assigned. `docs_ready` property: `docs_count >= docs_required`.

### Storage Time Calculation (HAWB)

Calculated from `scan_into_bond`:
- Free storage: 30 days
- Paid storage: begins day 31
- Total limit: 120 days
- `is_paid_storage`, `storage_days_left` — computed properties, not stored fields.

## Known Issues

**Cargo.change_status() is dead code** (models.py ~line 393): The method body is indented inside `status_timer_display` property after a return statement, making it unreachable. There is no `def change_status` declaration. Admin bulk actions (`action_hold`, `action_exam`, `action_release`) call `cargo.change_status()` which will raise AttributeError at runtime.

## URL Structure

Root: `cargotrack/urls.py` — auth views (`/login/`, `/logout/`), admin (`/admin/`), includes `cargo.urls` at `/`.
App: `cargo/urls.py` — 18 routes. Key patterns:
- `/` — dashboard (Kanban board)
- `/cargo/<awb_number>/` — MAWB detail
- `/cargo/<awb_number>/stage/` — POST to change stage
- `/hawb/<int:hawb_id>/` — HAWB detail with goods and documents
- `/hawbs/` — global HAWB list
- `/export/`, `/hawbs/export/` — Excel exports

## Query Patterns in Views

Views use `select_related('warehouse')` for FK joins and `.annotate(Count, Sum)` for aggregations. Dashboard loads all Cargo objects and filters `is_problematic` in Python (not DB-level). List views are capped without pagination (cargo: 300, HAWB: 500).

## Choices Constants

All defined as module-level lists in `cargo/models.py`: `ENTRY_TYPE_CHOICES` (16), `SHP_TYPE_CHOICES` (4), `STAGE_CHOICES` (6), `RTO_REASON_CHOICES` (11), `CPC_CODE_CHOICES` (15), `TRANSPORT_MODE_CHOICES` (6), `CURRENCY_CHOICES` (5), `ROLE_CHOICES` (5). HAWB-specific choices are class-level on `HouseWaybill`.
