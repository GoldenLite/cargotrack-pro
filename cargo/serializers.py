"""
DRF-сериализаторы для API CargoTrack Pro
"""
from rest_framework import serializers
from .models import Cargo, HouseWaybill, Warehouse


class WarehouseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Warehouse
        fields = ['id', 'name', 'license_number', 'city', 'iata_code']


class HouseWaybillSerializer(serializers.ModelSerializer):
    logistics_status_display = serializers.CharField(read_only=True)
    customs_status_label = serializers.CharField(read_only=True)
    full_status_display = serializers.CharField(read_only=True)
    cdek_status_display = serializers.CharField(read_only=True)

    class Meta:
        model = HouseWaybill
        fields = [
            'id', 'hawb_number', 'mawb',
            'consignee_name', 'consignee_city',
            'cargo_type', 'shipment_type',
            'weight', 'pieces_declared',
            'invoice_value', 'invoice_currency',
            'logistics_status', 'logistics_status_display',
            'customs_status', 'customs_status_label', 'full_status_display',
            'customs_declaration_number',
            'scan_into_bond', 'release_date',
            'cdek_number', 'cdek_status_code', 'cdek_status_name',
            'cdek_status_display', 'cdek_status_date', 'cdek_synced_at',
            'created_at', 'updated_at',
        ]


class CargoListSerializer(serializers.ModelSerializer):
    """Лёгкий сериализатор для списков"""
    stage_display = serializers.CharField(read_only=True)
    warehouse_name = serializers.CharField(read_only=True)
    hawbs_count = serializers.SerializerMethodField()

    class Meta:
        model = Cargo
        fields = [
            'id', 'awb_number', 'shp_type',
            'stage', 'stage_display', 'is_draft',
            'flight_number', 'departure_date', 'flight_date',
            'departure_iata', 'arrival_iata',
            'weight', 'pieces_declared',
            'invoice_value', 'invoice_currency',
            'warehouse_name', 'warehouse_license',
            'customs_declaration_number',
            'hawbs_count',
            'created_at', 'updated_at',
        ]

    def get_hawbs_count(self, obj):
        return obj.hawbs.count()


class CargoDetailSerializer(CargoListSerializer):
    """Полный сериализатор с накладными"""
    hawbs = HouseWaybillSerializer(many=True, read_only=True)
    warehouse = WarehouseSerializer(read_only=True)

    class Meta(CargoListSerializer.Meta):
        fields = CargoListSerializer.Meta.fields + [
            'description', 'description_ru',
            'cpc_code', 'transportation_mode',
            'movement_number',
            'customs_value_rub', 'duty_amount',
            'bond_location', 'scan_into_bond', 'scan_out_of_bond',
            'entry_date', 'release_date',
            'is_transit', 'rto_reason',
            'warehouse', 'hawbs',
        ]
