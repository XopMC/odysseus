import asyncio
import base64
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


class HostExecutionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('host_transport_test', self.root / 'src/host_execution.py')
        self.host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.host)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def helper(self, tool, content, timeout=3):
        result = subprocess.run([sys.executable, str(self.root / 'scripts/host_exec.py')],
                                input=json.dumps({'tool': tool, 'content': content,
                                                  'cwd': self.tmp.name, 'timeout': timeout}),
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_disabled_and_exact_owner_only(self):
        with patch.dict(os.environ, {'ODYSSEUS_HOST_ENABLED': '1', 'ODYSSEUS_HOST_OWNER': 'xopmc'}):
            self.assertTrue(self.host.enabled_for('xopmc'))
            for other in ('XopMC', '', None, 'guest'):
                self.assertFalse(self.host.enabled_for(other))
        with patch.dict(os.environ, {'ODYSSEUS_HOST_ENABLED': '0', 'ODYSSEUS_HOST_OWNER': 'xopmc'}):
            self.assertFalse(self.host.enabled_for('xopmc'))

    def test_registered_host_diagnostics_are_fixed_read_only_tools(self):
        for tool in ('inspect_process', 'inspect_port', 'tail_log'):
            self.assertIn(tool, self.host.TOOLS)
            request = self.host.request_for(tool, '{}')
            self.assertEqual(request['tool'], tool)
            self.assertEqual(request['content'], '{}')

    def test_host_toolchain_inventory_is_fixed_and_does_not_probe_arbitrary_network(self):
        self.assertIn('inspect_toolchain', self.host.TOOLS)
        request = self.host.request_for('inspect_toolchain', '{}')
        self.assertEqual(request['tool'], 'inspect_toolchain')
        result = self.helper('inspect_toolchain', '{}')
        self.assertEqual(result['exit_code'], 0)
        self.assertIn('python', result['tools'])
        self.assertEqual(result['network']['status'], 'not_checked')
        self.assertEqual(self.helper('inspect_toolchain', '{"endpoint_id":"registered"}')['code'],
                         'not_supported_by_route')
        with self.assertRaises(ValueError):
            self.host.request_for('inspect_toolchain', '{"url":"http://127.0.0.1"}')

    def test_lost_host_reply_is_unknown_outcome_and_never_replayed(self):
        calls = []
        async def lost(op, args, owner, scope):
            calls.append(op)
            return {'ok': False, 'error': 'ssh private detail'}
        with patch.dict(os.environ, {'ODYSSEUS_HOST_ENABLED': '1', 'ODYSSEUS_HOST_OWNER': 'alice'}), \
             patch('src.team_host.call', side_effect=lost):
            result = asyncio.run(self.host.execute(
                'write_file', '{"path":"safe.txt","content":"content"}',
                owner='alice', session_id='session-1'))
        self.assertEqual(calls, ['file.call'])
        self.assertEqual(result['code'], 'unknown_outcome')
        self.assertTrue(result['outcome_unknown'])
        self.assertFalse(result['retryable'])
        self.assertNotIn('private detail', result['error'])

    def test_agent_file_mutations_use_durable_owner_session_checkpoint_route(self):
        calls = []

        async def checkpointed(op, args, owner, scope):
            calls.append((op, args, owner, scope))
            return {'ok': True, 'result': {
                'output': 'Edited safely', 'exit_code': 0, 'checkpoint_id': 'cp-1',
                'file_checkpoint': {'id': 'cp-1', 'status': 'applied', 'files': []},
            }}

        with patch.dict(os.environ, {'ODYSSEUS_HOST_ENABLED': '1', 'ODYSSEUS_HOST_OWNER': 'alice'}), \
             patch('src.team_host.call', side_effect=checkpointed):
            result = asyncio.run(self.host.execute(
                'edit_file', '{"path":"/work/a.py","old_string":"a","new_string":"b",'
                '"expected_sha256":"' + 'a' * 64 + '"}',
                owner='alice', session_id='session-1', run_id='run-1'))

        self.assertEqual(result['checkpoint_id'], 'cp-1')
        self.assertEqual(calls[0][0:1], ('file.call',))
        self.assertEqual(calls[0][2:], ('alice', 'session-1'))
        self.assertEqual(calls[0][1]['tool'], 'edit_file')
        self.assertEqual(calls[0][1]['run_id'], 'run-1')
        self.assertEqual(calls[0][1]['content'], '{"path":"/work/a.py","old_string":"a","new_string":"b",'
                         '"expected_sha256":"' + 'a' * 64 + '"}')

    def test_agent_checkpoint_rollback_uses_same_owner_session_scope(self):
        calls = []

        async def rollback(op, args, owner, scope):
            calls.append((op, args, owner, scope))
            return {'ok': True, 'result': {'status': 'rolled_back', 'files': ['/work/a.py']}}

        with patch.dict(os.environ, {'ODYSSEUS_HOST_ENABLED': '1', 'ODYSSEUS_HOST_OWNER': 'alice'}), \
             patch('src.team_host.call', side_effect=rollback):
            result = asyncio.run(self.host.execute(
                'rollback_file_checkpoint', '{"checkpoint_id":"cp-1",'
                '"expected_sha256":{"/work/a.py":"' + 'b' * 64 + '"}}',
                owner='alice', session_id='session-1'))

        self.assertEqual(result['status'], 'rolled_back')
        self.assertEqual(calls[0][0], 'file.rollback')
        self.assertEqual(calls[0][2:], ('alice', 'session-1'))

    def test_native_rollback_tool_arguments_survive_schema_conversion(self):
        from src.tool_parsing import TOOL_TAGS
        from src.tool_schemas import function_call_to_tool_block

        args = {'checkpoint_id': 'cp-1', 'expected_sha256': {'/work/a.py': 'b' * 64}}
        block = function_call_to_tool_block('rollback_file_checkpoint', json.dumps(args))

        self.assertIn('rollback_file_checkpoint', TOOL_TAGS)
        self.assertIsNotNone(block)
        self.assertEqual(block.tool_type, 'rollback_file_checkpoint')
        self.assertEqual(json.loads(block.content), args)

    def test_lost_rollback_ack_is_unknown_outcome_and_not_retryable(self):
        async def lost(_op, _args, _owner, _scope):
            return {'ok': False, 'error': 'private ssh detail'}

        with patch.dict(os.environ, {'ODYSSEUS_HOST_ENABLED': '1', 'ODYSSEUS_HOST_OWNER': 'alice'}), \
             patch('src.team_host.call', side_effect=lost):
            result = asyncio.run(self.host.execute(
                'rollback_file_checkpoint', json.dumps({
                    'checkpoint_id': 'cp-1', 'expected_sha256': {'/work/a': 'a' * 64}}),
                owner='alice', session_id='session-1'))

        self.assertEqual(result['code'], 'unknown_outcome')
        self.assertTrue(result['outcome_unknown'])
        self.assertFalse(result['retryable'])
        self.assertNotIn('private ssh detail', result['error'])

    def test_host_schema_scope_is_truthful_and_not_global(self):
        schemas = [{'type': 'function', 'function': {'name': 'get_workspace', 'description': 'File tools are confined to it', 'parameters': {}}}]
        with patch.dict(os.environ, {'ODYSSEUS_HOST_ENABLED': '1', 'ODYSSEUS_HOST_OWNER': 'xopmc'}):
            changed = self.host.adapt_schemas(schemas, 'xopmc')
            self.assertIn('NOT a filesystem boundary', changed[0]['function']['description'])
            self.assertEqual(schemas[0]['function']['description'], 'File tools are confined to it')
            self.assertIs(self.host.adapt_schemas(schemas, 'other'), schemas)

    def test_strict_ssh_argv_and_content_never_remote_command(self):
        with patch.dict(os.environ, {'ODYSSEUS_HOST_TARGET': 'xopmc@192.168.50.6',
                                     'ODYSSEUS_HOST_HELPER': '/home/xopmc/a b/helper.py'}):
            argv = self.host.ssh_argv()
            self.assertIn('StrictHostKeyChecking=yes', argv)
            self.assertIn('BatchMode=yes', argv)
            self.assertEqual(argv[-1], "python3 '/home/xopmc/a b/helper.py'")
            payload = 'echo "$(touch /tmp/not-run)"; exit 7'
            request = self.host.request_for('bash', payload)
            with patch.object(self.host.subprocess, 'run', return_value=subprocess.CompletedProcess(argv, 0, '{"exit_code":0,"output":"ok"}', '')) as run:
                self.assertEqual(self.host.run_request(request)['output'], 'ok')
                self.assertNotIn(payload, run.call_args.args[0])
                self.assertEqual(json.loads(run.call_args.kwargs['input'])['content'], payload)
        with patch.dict(os.environ, {'ODYSSEUS_HOST_TARGET': '-oProxyCommand=oops'}):
            with self.assertRaises(ValueError):
                self.host.ssh_argv()

    def test_timeout_is_reported_and_sudo_blocked(self):
        with patch.object(self.host, 'run_request', side_effect=subprocess.TimeoutExpired('ssh', 1)):
            result = asyncio.run(self.host.execute('bash', 'true'))
            self.assertEqual(result['exit_code'], 1)
        with self.assertRaisesRegex(ValueError, '/host-access'):
            self.host.request_for('bash', 'sudo apt update')
        self.assertEqual(self.helper('bash', 'sudo true')['exit_code'], 1)
        with self.assertRaisesRegex(ValueError, 'must be a string'):
            self.host.request_for('bash', {'command': 'true'})

    def test_real_host_shell_python_and_file_seam(self):
        self.assertEqual(self.helper('bash', 'printf hello')['output'], 'hello')
        self.assertEqual(self.helper('python', 'print(6 * 7)')['output'], '42\n')
        self.assertEqual(self.helper('write_file', {'path': 'a', 'content': 'host data'})['exit_code'], 0)
        self.assertEqual((Path(self.tmp.name) / 'a').read_text(), 'host data')
        self.assertEqual(self.helper('read_file', {'path': 'a'})['output'], 'host data')
        self.assertEqual(self.helper('bash', 'exit 9')['exit_code'], 9)
        self.assertEqual(self.helper('search_files', {'pattern': 'host data',
                                                      'path': str(Path(self.tmp.name) / 'a')})['files'],
                         [str(Path(self.tmp.name) / 'a')])
        self.assertEqual(self.helper('list_tree', {'path': self.tmp.name})['entries'][0]['path'], 'a')

    def test_real_host_helper_typed_git_round_trip(self):
        root = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        (root / 'sample.txt').write_text('before\n')
        subprocess.run(['git', '-C', str(root), 'add', 'sample.txt'], check=True)
        subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.test', 'commit', '-qm', 'initial'], check=True)
        (root / 'sample.txt').write_text('after\n')
        status = self.helper('git_status', {'path': str(root)})
        self.assertEqual(status['exit_code'], 0)
        self.assertEqual(status['files'][0]['unstaged'], 'modified')
        diff = self.helper('git_diff', {'path': str(root), 'file': 'sample.txt'})
        self.assertEqual(diff['exit_code'], 0)
        self.assertIn('+after', diff['patch'])
        log = self.helper('git_log', {'path': str(root), 'limit': 1})
        self.assertEqual(log['exit_code'], 0)
        self.assertEqual(log['commits'][0]['subject'], 'initial')

    def test_real_host_verification_profiles_and_timeout(self):
        root = Path(self.tmp.name)
        (root / 'package.json').write_text(json.dumps({'scripts': {
            'test': 'node -e "console.log(\'failed test\'); process.exit(7)"',
            'lint': 'node -e "console.log(\'lint ok\')"',
            'other': 'node -e "process.exit(99)"',
        }}))
        bad = self.helper('run_tests', json.dumps({'profile': 'other'}))
        self.assertEqual(bad['code'], 'not_found')
        discovered = self.helper('run_tests', json.dumps({'profile': 'list'}))
        self.assertEqual(discovered['available_profiles'], ['npm_test'])
        self.assertEqual(discovered['code'], 'ok')
        self.assertEqual(discovered['exit_code'], 0)
        lint_profiles = self.helper('run_lint', json.dumps({'profile': 'list'}))
        self.assertEqual(lint_profiles['available_profiles'], ['npm_lint'])
        self.assertEqual(lint_profiles['exit_code'], 0)
        failed = self.helper('run_tests', json.dumps({'profile': 'npm_test'}))
        self.assertEqual(failed['exit_code'], 7)
        self.assertEqual(failed['code'], 'failed')
        self.assertIn('failed test', failed['full_output'])
        passed = self.helper('run_lint', '{}')
        self.assertEqual(passed['exit_code'], 0)
        self.assertEqual(passed['code'], 'ok')
        (root / 'package.json').write_text(json.dumps({'scripts': {
            'test': 'node -e "setTimeout(() => {}, 10000)"'}}))
        timed = self.helper('run_tests', json.dumps({'timeout_seconds': 1}), timeout=3)
        self.assertEqual(timed['exit_code'], 124)
        self.assertTrue(timed['timed_out'])
        self.assertEqual(timed['code'], 'timeout')

    def test_host_failed_verification_is_owner_scoped_artifact(self):
        from src import observation_pack
        root = Path(self.tmp.name)
        (root / 'package.json').write_text(json.dumps({'scripts': {
            'test': 'node -e "console.log(\'host failure\'); process.exit(2)"'}}))
        def local_transport(request):
            return self.helper(request['tool'], request['content'])
        with patch.object(self.host, 'run_request', side_effect=local_transport), \
             patch.object(observation_pack, 'DATA_DIR', self.tmp.name):
            run_id = 'a' * 32
            result = asyncio.run(self.host.execute('run_tests', '{}', owner='alice', session_id='one', run_id=run_id))
            self.assertEqual(result['exit_code'], 2)
            self.assertIn('artifact', result)
            self.assertNotIn('full_output', result)
            artifact_id = result['artifact']['id']
            self.assertIn('host failure', observation_pack.recall('alice', 'one', artifact_id, 0, run_id=run_id)['text'])
            with self.assertRaises(PermissionError):
                observation_pack.recall('alice', 'one', artifact_id, 0, run_id='b' * 32)
            with self.assertRaises(FileNotFoundError):
                observation_pack.recall('bob', 'one', artifact_id, 0)

    def test_large_host_read_archives_without_sending_whole_file_to_model(self):
        from src import observation_pack
        path = Path(self.tmp.name) / 'large.txt'
        content = 'host line\n' * 12_000
        path.write_text(content)
        def local_transport(request):
            return self.helper(request['tool'], request['content'])
        with patch.object(self.host, 'run_request', side_effect=local_transport), \
             patch.object(observation_pack, 'DATA_DIR', self.tmp.name):
            run_id = 'a' * 32
            result = asyncio.run(self.host.execute('read_file', json.dumps({'path': str(path)}),
                                                   owner='alice', session_id='one', run_id=run_id))
            self.assertEqual(result['exit_code'], 0)
            self.assertTrue(result['truncated'])
            self.assertLess(len(result['output']), 3000)
            self.assertIn('read_tool_artifact', result['output'])
            self.assertIn('artifact_id', result)
            self.assertIn('host line', observation_pack.recall('alice', 'one', result['artifact_id'], 0, run_id=run_id)['text'])
            with self.assertRaises(PermissionError):
                observation_pack.recall('alice', 'one', result['artifact_id'], 0, run_id='b' * 32)
            with self.assertRaises(FileNotFoundError):
                observation_pack.recall('bob', 'one', result['artifact_id'], 0)

    def test_host_artifact_rejects_changed_file_and_anonymous_scope(self):
        from scripts import host_files
        path = Path(self.tmp.name) / 'large.txt'
        path.write_text('a' * 90_000)
        calls = []
        def changed_transport(request):
            calls.append(request['tool'])
            result = host_files.handle(request['tool'], request['content'], request['cwd'])
            if request['tool'] == 'read_file_chunk':
                result['sha256'] = '0' * 64
            return result
        with patch.object(self.host, 'run_request', side_effect=changed_transport):
            result = asyncio.run(self.host.execute('read_file', str(path), owner='alice', session_id='one'))
            self.assertEqual(result['exit_code'], 0)
            self.assertTrue(result['artifact_unavailable'])
            self.assertNotIn('artifact_id', result)
            calls.clear()
            anonymous = asyncio.run(self.host.execute('read_file', str(path)))
            self.assertNotIn('artifact_id', anonymous)
            self.assertEqual(calls, ['read_file'])

    def test_real_output_cap_and_timeout(self):
        output = self.helper('python', 'print("x" * 100000)')
        self.assertTrue(output['truncated'])
        self.assertLess(len(output['output']), 60200)
        timed = self.helper('bash', 'sleep 20', timeout=1)
        self.assertTrue(timed['timed_out'])
        self.assertEqual(timed['exit_code'], 124)

    def test_background_command_encodes_not_interpolates(self):
        content = 'echo "quoted"; $(not_executed_locally)'
        argv = shlex.split(self.host.background_command('bash', content))
        self.assertEqual(argv[-2], '--background')
        request = json.loads(base64.urlsafe_b64decode(argv[-1]))
        self.assertEqual(request['content'], content)
        self.assertEqual(request['timeout'], 3500)

    def test_channel_close_terminates_host_child(self):
        process = subprocess.Popen([sys.executable, str(self.root / 'scripts/host_exec.py')],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True)
        try:
            process.stdin.write(json.dumps({'tool': 'bash', 'content': 'sleep 30',
                                            'cwd': self.tmp.name, 'timeout': 30}))
            process.stdin.close()
            process.stdout.close()
            self.assertNotEqual(process.wait(timeout=5), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


if __name__ == '__main__':
    unittest.main()
