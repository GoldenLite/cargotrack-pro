from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from cargo.sla import compute_sla_state, invalidate_policy_cache
from cargo.models import SLAPolicy, Workflow, WorkflowStep


class SLAComputeTests(TestCase):
    def setUp(self):
        invalidate_policy_cache()
        SLAPolicy.objects.create(
            name='Cargo arrived 2h',
            entity_type='cargo',
            status_field='stage',
            status_value='ARRIVED',
            hours=Decimal('2'),
            warning_threshold_pct=75,
            is_active=True,
        )
        invalidate_policy_cache()

    def test_compute_sla_state_within_window(self):
        now = timezone.now()
        changed = now - timedelta(hours=1)
        state = compute_sla_state('cargo', 'stage', 'ARRIVED', changed, now=now)
        self.assertIsNotNone(state)
        self.assertFalse(state['breached'])
        # 2h limit, 1h passed → ~3600 sec осталось (с поправкой на округление)
        self.assertAlmostEqual(state['remaining_seconds'], 3600, delta=5)
        self.assertEqual(state['source'], 'policy')

    def test_compute_sla_state_breached(self):
        now = timezone.now()
        changed = now - timedelta(hours=3)
        state = compute_sla_state('cargo', 'stage', 'ARRIVED', changed, now=now)
        self.assertIsNotNone(state)
        self.assertTrue(state['breached'])
        self.assertLess(state['remaining_seconds'], 0)

    def test_workflow_step_override_wins_over_policy(self):
        wf = Workflow.objects.create(
            name='WF', entity_type='cargo', auto_start=False, is_active=True,
        )
        step = WorkflowStep.objects.create(
            workflow=wf, name='Override', step_type='stage', stage='ARRIVED',
            pos_x=0, pos_y=0, color='#000', order=0,
            sla_hours_override=Decimal('1'),
        )
        now = timezone.now()
        changed = now - timedelta(minutes=30)
        state = compute_sla_state(
            'cargo', 'stage', 'ARRIVED', changed,
            workflow_step=step, now=now,
        )
        self.assertEqual(state['source'], 'workflow_step')
        self.assertEqual(state['hours'], 1.0)
        # 1h limit, 30min passed → ~1800s remaining
        self.assertAlmostEqual(state['remaining_seconds'], 1800, delta=5)
