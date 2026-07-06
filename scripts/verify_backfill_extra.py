"""Extra diagnostics for the verification."""
import os, sys, django
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cargotrack.settings")
django.setup()

from django.db.models import Min, Max, Count
from cargo.models import AltaInboxMessage, HouseWaybill, HawbCustomsRequest, HawbDeclarationAttempt

# range of prepared_at for old messages
old = AltaInboxMessage.objects.filter(prepared_at__lt="2026-05-21")
agg = old.aggregate(mn=Min("prepared_at"), mx=Max("prepared_at"))
print(f"Old prepared_at range: {agg['mn']} .. {agg['mx']}")
print(f"Old total: {old.count()}")

# matched vs unmatched among old
matched = old.filter(hawb__isnull=False).count()
unmatched = old.filter(hawb__isnull=True).count()
print(f"Old matched to HAWB     : {matched}")
print(f"Old NOT matched to HAWB : {unmatched}")

# breakdown by msg_type for old messages
print("\nOld messages by msg_type (top 15):")
for row in old.values("msg_type").annotate(n=Count("id")).order_by("-n")[:15]:
    print(f"  {row['msg_type']:<25} {row['n']:>6}")

# breakdown by msg_kind
print("\nOld messages by msg_kind (top 15):")
for row in old.values("msg_kind").annotate(n=Count("id")).order_by("-n")[:15]:
    print(f"  {str(row['msg_kind']):<25} {row['n']:>6}")

# created_at on old AltaInboxMessage (when they were ingested into our DB)
if any(f.name == "created_at" for f in AltaInboxMessage._meta.get_fields()):
    cag = old.aggregate(mn=Min("created_at"), mx=Max("created_at"))
    print(f"\nOld msgs created_at in DB: {cag['mn']} .. {cag['mx']}")

# created_at range for ALL AltaInboxMessage today
from datetime import datetime
today_start = datetime(2026,6,3,0,0,0)
if any(f.name == "created_at" for f in AltaInboxMessage._meta.get_fields()):
    today = AltaInboxMessage.objects.filter(created_at__gte=today_start)
    print(f"\nAIM created_at>=2026-06-03 00:00 : {today.count()}")
    tag = today.aggregate(mn=Min("created_at"), mx=Max("created_at"))
    print(f"   range: {tag['mn']} .. {tag['mx']}")

# HawbCustomsRequest: how many created today
if any(f.name == "created_at" for f in HawbCustomsRequest._meta.get_fields()):
    print(f"HawbCustomsRequest created today: {HawbCustomsRequest.objects.filter(created_at__gte=today_start).count()}")

# HawbDeclarationAttempt: created today
if any(f.name == "created_at" for f in HawbDeclarationAttempt._meta.get_fields()):
    print(f"HawbDeclarationAttempt created today: {HawbDeclarationAttempt.objects.filter(created_at__gte=today_start).count()}")
