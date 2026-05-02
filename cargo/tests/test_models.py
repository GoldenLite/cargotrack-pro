from django.db import IntegrityError
from django.test import TestCase

from cargo.models import Cargo, HouseWaybill, Label


class CargoModelTests(TestCase):
    def test_cargo_create_minimal(self):
        c = Cargo.objects.create(awb_number='123-12345670')
        self.assertEqual(c.awb_number, '123-12345670')
        self.assertEqual(c.stage, 'DRAFT')
        self.assertEqual(c.status, 'CNPK')
        self.assertEqual(c.pieces_declared, 0)

    def test_cargo_awb_unique(self):
        Cargo.objects.create(awb_number='123-12345671')
        with self.assertRaises(IntegrityError):
            Cargo.objects.create(awb_number='123-12345671')

    def test_cargo_set_stage_updates_timestamp(self):
        c = Cargo.objects.create(awb_number='123-12345672')
        self.assertIsNone(c.stage_changed_at)
        c.set_stage('FORMED')
        c.refresh_from_db()
        self.assertEqual(c.stage, 'FORMED')
        self.assertIsNotNone(c.stage_changed_at)


class HouseWaybillModelTests(TestCase):
    def test_hawb_belongs_to_cargo_and_set_null_on_delete(self):
        # HAWB прикрепляется к партии только в статусе AT_ORIGIN_WH (валидация
        # модели). При удалении партии HAWB не каскадится — становится
        # standalone (mawb=NULL, статус сбрасывается до AT_ORIGIN_WH).
        cargo = Cargo.objects.create(awb_number='123-12345673')
        hawb = HouseWaybill.objects.create(
            mawb=cargo, hawb_number='HAWB-001',
            logistics_status='AT_ORIGIN_WH',
        )
        self.assertEqual(hawb.mawb_id, cargo.id)
        cargo.delete()
        hawb.refresh_from_db()
        self.assertIsNone(hawb.mawb_id)
        self.assertEqual(hawb.logistics_status, 'AT_ORIGIN_WH')


class LabelModelTests(TestCase):
    def test_label_m2m_with_cargo_and_hawb(self):
        cargo = Cargo.objects.create(awb_number='123-12345674')
        hawb = HouseWaybill.objects.create(
            hawb_number='HAWB-002', logistics_status='AT_ORIGIN_WH',
        )
        red = Label.objects.create(name='Срочно', color='#ff0000')
        green = Label.objects.create(name='Готово', color='#00ff00')
        cargo.labels.set([red, green])
        hawb.labels.add(red)
        self.assertEqual(cargo.labels.count(), 2)
        self.assertEqual(red.cargos.count(), 1)
        self.assertEqual(red.hawbs.count(), 1)
        self.assertEqual(green.hawbs.count(), 0)
