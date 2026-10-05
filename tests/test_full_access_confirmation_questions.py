import json

import pytest

from src.agent_tools.interaction_tools import AskUserTool


@pytest.mark.asyncio
@pytest.mark.parametrize('question,options', [
    ('Подтвердите запуск тестов по текущему плану?', ['Да', 'Нет']),
    ('May I update the current plan?', ['Yes, continue', 'No, wait']),
])
async def test_full_access_does_not_enter_wait_for_redundant_permission(question, options):
    _, result = await AskUserTool().execute(json.dumps({'question': question, 'options': options}),
                                           {'access_mode': 'full_access'})
    assert result['confirmation_skipped'] is True
    assert 'ask_user' not in result
    assert 'grants no new permission' in result['output']


@pytest.mark.asyncio
async def test_explicit_confirmation_is_redirected_without_manufacturing_an_answer():
    _, result = await AskUserTool().execute(json.dumps({
        'question': 'Execute this step?', 'purpose': 'confirmation',
        'options': ['Execute', 'Later'],
    }), {'access_mode': 'full_access'})
    assert 'ask_user' not in result and 'answer' not in result
    assert result['exit_code'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('ctx,purpose', [
    ({'access_mode': 'ask_important'}, 'confirmation'),
    ({'access_mode': 'ask_every_time'}, 'confirmation'),
    ({'access_mode': 'full_access', 'delegated_credential': True}, 'confirmation'),
    ({'access_mode': 'full_access'}, 'clarification'),
])
async def test_real_clarification_and_nonfull_or_delegated_modes_still_ask(ctx, purpose):
    _, result = await AskUserTool().execute(json.dumps({
        'question': 'Confirm the target database?', 'purpose': purpose,
        'options': ['Yes', 'No'],
    }), ctx)
    assert 'ask_user' in result


@pytest.mark.asyncio
async def test_missing_design_information_is_not_autoanswered_under_full_access():
    _, result = await AskUserTool().execute(json.dumps({
        'question': 'Which database is required?', 'options': ['SQLite', 'PostgreSQL'],
    }), {'access_mode': 'full_access'})
    assert [o['label'] for o in result['ask_user']['options']] == ['SQLite', 'PostgreSQL']


@pytest.mark.asyncio
async def test_legacy_target_confirmation_is_not_mistaken_for_execution_permission():
    _, result = await AskUserTool().execute(json.dumps({
        'question': 'Confirm the target database?', 'options': ['Yes', 'No'],
    }), {'access_mode': 'full_access'})
    assert 'ask_user' in result
