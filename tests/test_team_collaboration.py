"""Pure native team-tool contract and bounded, backward-only plan validation."""
import json
import unittest

from src.team_collaboration import schemas, validate_tool_arguments, PlanAccumulator


def subtask(**changes):
    return {'name': 'Inspect parser', 'objective': 'Inspect parser edge cases',
            'acceptance': 'List concrete failing inputs and tests', 'participant': 0,
            'depends_on': [], 'write_scope': ['src/parser.py'], **changes}


class TeamCollaborationTests(unittest.TestCase):
    def test_common_workers_have_communication_but_no_spawn_tools(self):
        tools = schemas()
        self.assertEqual({item['function']['name'] for item in tools},
                         {'team_status', 'team_result', 'team_message'})
        self.assertTrue(all(item['type'] == 'function' for item in tools))
        self.assertTrue(all(item['function']['parameters']['additionalProperties'] is False for item in tools))

    def test_planner_adds_only_bounded_plan_tools(self):
        tools = {item['function']['name']: item for item in schemas(planner=True)}
        self.assertEqual(set(tools), {'team_status', 'team_result', 'team_message',
                                     'team_create_subtask', 'team_finish_plan'})
        fields = tools['team_create_subtask']['function']['parameters']['properties']
        self.assertEqual(fields['participant']['type'], 'integer')
        self.assertEqual(fields['depends_on']['items']['type'], 'integer')
        self.assertNotIn('endpoint_id', fields)

    def test_schema_outputs_are_independently_mutable(self):
        first = schemas(planner=True)
        first[0]['function']['parameters']['properties']['injected'] = {'type': 'string'}
        self.assertNotIn('injected', schemas()[0]['function']['parameters']['properties'])

    def test_worker_cannot_invoke_planning_even_with_valid_payload(self):
        for name, args in [('team_create_subtask', subtask()), ('team_finish_plan', {})]:
            with self.assertRaises(PermissionError):
                validate_tool_arguments(name, args)

    def test_common_arguments_accept_only_bounded_explicit_fields(self):
        self.assertEqual(validate_tool_arguments('team_status', {}), {})
        self.assertEqual(validate_tool_arguments('team_result', {'worker_id': 'w-1'}), {'worker_id': 'w-1'})
        message = {'worker_id': 'w-1', 'text': 'Parser test fails on empty input'}
        self.assertEqual(validate_tool_arguments('team_message', message), message)
        for name, args in [('team_status', {'all_context': True}),
                           ('team_result', {'worker_id': ''}),
                           ('team_message', {'worker_id': 'w', 'text': 'x' * 4001}),
                           ('team_message', {'worker_id': 'w', 'text': ' ', 'permissions': {}}),
                           ('team_spawn', {})]:
            with self.assertRaises(ValueError):
                validate_tool_arguments(name, args)

    def test_constructor_rejects_empty_or_non_integer_pool(self):
        for count in [0, True, 1.0, '2']:
            with self.assertRaises(ValueError):
                PlanAccumulator(count)

    def test_accumulates_backward_dag_in_existing_runtime_shape(self):
        plan = PlanAccumulator(2)
        self.assertEqual(plan.add_subtask(subtask())['index'], 0)
        plan.add_subtask(subtask(name='Implement fix', participant=1, depends_on=[0]))
        plan.add_subtask(subtask(name='Verify integration', depends_on=[0, 1]))
        result = plan.finish_plan()
        self.assertEqual(len(result['tasks']), 3)
        self.assertEqual(result['tasks'][1]['participant'], 1)
        self.assertEqual(result['tasks'][2]['depends_on'], [0, 1])
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_large_pool_and_plan_survive_checkpoint(self):
        plan = PlanAccumulator(24)
        for index in range(24):
            plan.add_subtask(subtask(name=f'Task {index}', participant=index,
                                     depends_on=list(range(index))))
        restored = PlanAccumulator(24, plan=plan.snapshot())
        self.assertEqual(restored.finish_plan(), plan.finish_plan())
        self.assertEqual(len(restored.finish_plan()['tasks']), 24)

    def test_self_forward_invalid_and_duplicate_dependencies_rejected(self):
        plan = PlanAccumulator(1)
        plan.add_subtask(subtask())
        for dependencies in [[1], [2], [-1], [True], [0.0], ['0'], [0, 0], '0', None]:
            with self.assertRaises(ValueError):
                plan.add_subtask(subtask(depends_on=dependencies))
            self.assertEqual(len(plan.snapshot()['tasks']), 1)

    def test_participant_cannot_escape_approved_pool(self):
        plan = PlanAccumulator(2)
        for participant in [-1, 2, 12, True, 1.0, '1', None]:
            with self.assertRaises(ValueError):
                plan.add_subtask(subtask(participant=participant))
        self.assertEqual(plan.snapshot(), {'tasks': []})

    def test_missing_fields_empty_text_and_authority_fields_rejected(self):
        plan = PlanAccumulator(1)
        for key in ['name', 'objective', 'acceptance', 'participant']:
            value = subtask()
            del value[key]
            with self.assertRaises(ValueError):
                plan.add_subtask(value)
        for value in [subtask(name=''), subtask(objective=' '), subtask(acceptance=None),
                      subtask(objective='x' * 8001), subtask(endpoint_id='unapproved'),
                      subtask(kind='planner'), subtask(permissions={'trusted_host': True})]:
            with self.assertRaises(ValueError):
                plan.add_subtask(value)

    def test_scope_is_bounded_data_not_a_new_permission_object(self):
        plan = PlanAccumulator(1)
        for scope in ['src', {'path': 'src'}, [None], ['\0'], ['x' * 1025], ['x'] * 33]:
            with self.assertRaises(ValueError):
                plan.add_subtask(subtask(write_scope=scope))
        plan.add_subtask(subtask(write_scope=[]))
        self.assertEqual(plan.finish_plan()['tasks'][0]['write_scope'], [])

    def test_payload_and_return_values_cannot_mutate_accumulated_plan(self):
        plan = PlanAccumulator(1)
        value = subtask()
        returned = plan.add_subtask(value)
        value['write_scope'].append('outside')
        returned['task']['write_scope'].append('also outside')
        snapshot = plan.snapshot()
        snapshot['tasks'][0]['depends_on'].append(0)
        self.assertEqual(plan.finish_plan()['tasks'][0]['write_scope'], ['src/parser.py'])
        self.assertEqual(plan.finish_plan()['tasks'][0]['depends_on'], [])

    def test_empty_plan_cannot_be_finished(self):
        with self.assertRaises(ValueError):
            PlanAccumulator(1).finish_plan()

    def test_checkpoint_restore_revalidates_every_task_and_copies_data(self):
        original = {'tasks': [subtask(), subtask(name='Review', depends_on=[0])]}
        restored = PlanAccumulator(1, plan=original)
        original['tasks'][0]['name'] = 'Changed elsewhere'
        self.assertEqual(restored.snapshot()['tasks'][0]['name'], 'Inspect parser')
        for invalid in [{'tasks': [subtask(depends_on=[0])]}, {'tasks': 'bad'},
                        {'tasks': [], 'permissions': {}}, {'tasks': [subtask(participant=1)]}]:
            with self.assertRaises(ValueError):
                PlanAccumulator(1, plan=invalid)


if __name__ == '__main__':
    unittest.main()
