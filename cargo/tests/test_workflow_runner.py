from django.test import TestCase

from cargo import workflow_runner
from cargo.models import (
    Cargo, Workflow, WorkflowStep, WorkflowInstance,
)


def _wf(name='Test WF', auto_start=True, conditions=None):
    return Workflow.objects.create(
        name=name,
        entity_type='cargo',
        is_active=True,
        auto_start=auto_start,
        trigger_conditions=conditions or {},
    )


def _step(workflow, name, stage, order=0):
    return WorkflowStep.objects.create(
        workflow=workflow,
        name=name,
        step_type='stage',
        stage=stage,
        pos_x=order * 200,
        pos_y=0,
        color='#3b5680',
        order=order,
    )


class WorkflowRunnerTests(TestCase):
    def test_auto_start_creates_instance_via_post_save_signal(self):
        # Cargo.save → post_save сигнал → start_for_entity. Прямой вызов уже
        # возвращает [] (есть активный), поэтому проверяем фактическое наличие
        # инстанса в БД.
        wf = _wf()
        _step(wf, 'Шаг 1', 'DRAFT', order=0)
        cargo = Cargo.objects.create(awb_number='999-00000001')
        self.assertEqual(
            WorkflowInstance.objects.filter(
                workflow=wf, entity_id=cargo.id, status='active'
            ).count(), 1
        )

    def test_inactive_workflow_does_not_start(self):
        wf = _wf(name='Disabled WF')
        wf.is_active = False
        wf.save()
        _step(wf, 'Шаг 1', 'DRAFT', order=0)
        cargo = Cargo.objects.create(awb_number='999-00000002')
        self.assertEqual(
            WorkflowInstance.objects.filter(workflow=wf, entity_id=cargo.id).count(),
            0,
        )

    def test_advance_on_status_change_moves_to_next_step(self):
        wf = _wf()
        s1 = _step(wf, 'Draft', 'DRAFT', order=0)
        s2 = _step(wf, 'Formed', 'FORMED', order=1)
        cargo = Cargo.objects.create(awb_number='999-00000003')
        workflow_runner.start_for_entity(cargo, 'cargo')
        inst = WorkflowInstance.objects.get(workflow=wf, entity_id=cargo.id)
        self.assertEqual(inst.current_step_id, s1.id)
        # Меняем stage напрямую и зовём advance — модель set_stage уже сама
        # дёргает advance, но здесь проверяем именно публичную функцию.
        cargo.stage = 'FORMED'
        cargo.save()
        workflow_runner.advance_on_status_change(cargo, 'cargo', 'stage', 'FORMED')
        inst.refresh_from_db()
        self.assertEqual(inst.current_step_id, s2.id)

    def test_cancel_instance_marks_cancelled(self):
        wf = _wf()
        _step(wf, 'Draft', 'DRAFT', order=0)
        cargo = Cargo.objects.create(awb_number='999-00000004')
        workflow_runner.start_for_entity(cargo, 'cargo')
        inst = WorkflowInstance.objects.get(workflow=wf, entity_id=cargo.id)
        ok, _ = workflow_runner.cancel_instance(inst.id)
        self.assertTrue(ok)
        inst.refresh_from_db()
        self.assertEqual(inst.status, 'cancelled')
