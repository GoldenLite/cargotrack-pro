"""Post-backfill verification: counts, deltas, spot-checks, auto-created EXPORT HAWB sanity.

Run from project root (cargotrack_pro) so cargo/cargotrack are on sys.path:
    uv run python scripts/verify_backfill.py
"""
import os
import sys
import django
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cargotrack.settings")
django.setup()

from django.db.models import Q
from cargo.models import (
    AltaInboxMessage,
    HouseWaybill,
    HawbCustomsRequest,
    HawbDeclarationAttempt,
)

# Baseline (from preflight)
BASE = {
    "AltaInboxMessage": 52381,
    "AltaInboxMessage.prepared_at<2026-05-21": 0,
    "HouseWaybill.decl!=''": 13293,
    "HouseWaybill.release_date NOT NULL": 3639,
    "HawbCustomsRequest": 242,
    "HawbDeclarationAttempt": 13474,
}


def main():
    print("=" * 80)
    print("POST-BACKFILL VERIFICATION")
    print("=" * 80)

    # --- 1. AltaInboxMessage counts ---
    aim_total = AltaInboxMessage.objects.count()
    aim_old = AltaInboxMessage.objects.filter(prepared_at__lt="2026-05-21").count()
    aim_11337 = AltaInboxMessage.objects.filter(msg_type="CMN.11337").count()
    aim_11350 = AltaInboxMessage.objects.filter(msg_type="CMN.11350").count()
    aim_creq = AltaInboxMessage.objects.filter(msg_kind="customs_request").count()

    # --- 2. HouseWaybill counts ---
    hwb_decl = HouseWaybill.objects.exclude(customs_declaration_number="").exclude(customs_declaration_number__isnull=True).count()
    hwb_release = HouseWaybill.objects.filter(release_date__isnull=False).count()

    # --- 3. Other ---
    creq_count = HawbCustomsRequest.objects.count()
    attempt_count = HawbDeclarationAttempt.objects.count()

    # --- Print table ---
    rows = [
        ("AltaInboxMessage total", BASE["AltaInboxMessage"], aim_total),
        ("AltaInboxMessage prepared_at<2026-05-21", BASE["AltaInboxMessage.prepared_at<2026-05-21"], aim_old),
        ("AltaInboxMessage msg_type=CMN.11337", "-", aim_11337),
        ("AltaInboxMessage msg_type=CMN.11350", "-", aim_11350),
        ("AltaInboxMessage msg_kind=customs_request", "-", aim_creq),
        ("HouseWaybill decl != ''", BASE["HouseWaybill.decl!=''"], hwb_decl),
        ("HouseWaybill release_date NOT NULL", BASE["HouseWaybill.release_date NOT NULL"], hwb_release),
        ("HawbCustomsRequest total", BASE["HawbCustomsRequest"], creq_count),
        ("HawbDeclarationAttempt total", BASE["HawbDeclarationAttempt"], attempt_count),
    ]
    print(f"\n{'Metric':<50} {'Baseline':>12} {'After':>12} {'Delta':>12}")
    print("-" * 90)
    for name, base, after in rows:
        if isinstance(base, int):
            delta = after - base
            print(f"{name:<50} {base:>12} {after:>12} {delta:>+12}")
        else:
            print(f"{name:<50} {base:>12} {after:>12} {'-':>12}")

    # --- Spot-check 5 random HAWBs whose decl was added by backfill ---
    # Strategy: find HAWBs whose inbox contains a message with prepared_at<2026-05-21
    # AND which have customs_declaration_number set. Pick 5 random.
    print("\n" + "=" * 80)
    print("SPOT-CHECK: 5 HAWBs with decl, having at least 1 inbox msg with prepared_at<2026-05-21")
    print("=" * 80)

    # Find HAWB IDs with old prepared_at via AltaInboxMessage.hawb relation
    # First check what relation exists
    field_names = [f.name for f in AltaInboxMessage._meta.get_fields()]
    # find FK to HouseWaybill or m2m
    hwb_link_field = None
    for f in AltaInboxMessage._meta.get_fields():
        try:
            rel = getattr(f, "related_model", None)
            if rel is HouseWaybill:
                hwb_link_field = f.name
                break
        except Exception:
            pass

    print(f"[debug] AltaInboxMessage->HouseWaybill link field: {hwb_link_field!r}")
    # Reverse from HouseWaybill side is `inbox_messages`
    reverse_name = "inbox_messages"

    if hwb_link_field:
        # Build the filter: hawbs that have an inbox row with prepared_at<2026-05-21
        filter_kw = {f"{reverse_name}__prepared_at__lt": "2026-05-21"}
        qs = (
            HouseWaybill.objects.filter(**filter_kw)
            .exclude(customs_declaration_number="")
            .exclude(customs_declaration_number__isnull=True)
            .distinct()
        )
        total_candidates = qs.count()
        print(f"[debug] Candidate HAWBs (have old inbox msg + decl set): {total_candidates}")

        sample = list(qs.order_by("?")[:5])
        for h in sample:
            print(f"\n  HAWB: {h.hawb_number}")
            print(f"    customs_status   : {getattr(h, 'customs_status', None)!r}")
            print(f"    customs_decl_no  : {h.customs_declaration_number!r}")
            print(f"    release_date     : {h.release_date!r}")
            print(f"    ed_status        : {getattr(h, 'ed_status', None)!r}")
            print(f"    shipment_type    : {getattr(h, 'shipment_type', None)!r}")
    else:
        print("[ERROR] could not find AltaInboxMessage->HouseWaybill link")

    # --- Auto-created EXPORT sanity ---
    print("\n" + "=" * 80)
    print("AUTO-CREATED EXPORT HAWB IN BACKFILL WINDOW")
    print("=" * 80)
    # Backfill ran between 16:25 and 16:40 local (approx); use 16:25 as cutoff
    from datetime import datetime
    backfill_start = datetime(2026, 6, 3, 16, 25, 0)
    has_created_at = any(f.name == "created_at" for f in HouseWaybill._meta.get_fields())
    print(f"[debug] HouseWaybill has created_at field: {has_created_at}")
    if has_created_at:
        export_new = HouseWaybill.objects.filter(
            shipment_type="EXPORT",
            created_at__gte=backfill_start,
        ).count()
        print(f"EXPORT HAWB created after {backfill_start.isoformat()}: {export_new}")
        if export_new > 0:
            print("  Sample (up to 10):")
            for h in HouseWaybill.objects.filter(
                shipment_type="EXPORT", created_at__gte=backfill_start
            )[:10]:
                print(f"    {h.hawb_number}  created_at={h.created_at}  decl={h.customs_declaration_number!r}")
    else:
        print("[skip] no created_at field on HouseWaybill")


if __name__ == "__main__":
    main()
