from django.test import TestCase

from cargo.cql_parser import parse_cql, CQLError
from cargo.models import Cargo, Label


class CQLParserTests(TestCase):
    def setUp(self):
        # Готовим тестовые данные для проверки фильтров через qs.filter(Q).
        self.draft = Cargo.objects.create(awb_number='100-00000001', stage='DRAFT')
        self.arrived = Cargo.objects.create(awb_number='100-00000002', stage='ARRIVED')
        self.released = Cargo.objects.create(awb_number='100-00000003', stage='RELEASED')

    def test_parse_simple_eq(self):
        q = parse_cql('stage = ARRIVED', entity_type='cargo')
        ids = set(Cargo.objects.filter(q).values_list('awb_number', flat=True))
        self.assertEqual(ids, {'100-00000002'})

    def test_parse_in_or_paren(self):
        q = parse_cql(
            '(stage IN (ARRIVED, RELEASED)) OR stage = DRAFT',
            entity_type='cargo',
        )
        self.assertEqual(Cargo.objects.filter(q).count(), 3)

    def test_parse_labels_in_m2m(self):
        urgent = Label.objects.create(name='urgent', color='#f00')
        self.arrived.labels.add(urgent)
        q = parse_cql('labels IN ("urgent")', entity_type='cargo')
        ids = set(Cargo.objects.filter(q).values_list('awb_number', flat=True))
        self.assertIn('100-00000002', ids)

    def test_parse_labels_is_null(self):
        # arrived has no labels yet — должно матчиться
        q = parse_cql('labels IS NULL', entity_type='cargo')
        ids = set(Cargo.objects.filter(q).values_list('awb_number', flat=True))
        self.assertIn('100-00000001', ids)
        self.assertIn('100-00000003', ids)

    def test_parse_invalid_raises(self):
        # Полностью кривой синтаксис должен бросать CQLError, а не возвращать
        # пустой Q (иначе фильтр молча покажет всё).
        with self.assertRaises(CQLError):
            parse_cql('stage = ', entity_type='cargo')
