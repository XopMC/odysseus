import base64
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


def load_runner():
    path = Path(__file__).resolve().parents[1] / 'scripts/host_runner.py'
    spec = importlib.util.spec_from_file_location('runner_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.module = load_runner()
        self.runner = self.module.Runner(self.root / 'state')
        self.addCleanup(self.runner.close)
        self.path_patch = patch.object(sys, 'path', [str(Path(__file__).resolve().parents[1] / 'scripts'), *sys.path])
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def call(self, op, args=None, owner='alice', scope='task'):
        return self.runner.handle({'op': op, 'args': args or {}, 'owner': owner, 'scope': scope})

    def wait(self, identity, condition=lambda r: r['status'] != 'running'):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            result = self.call('terminal.poll', {'id': identity})['result']
            if condition(result):
                return result
            time.sleep(.03)
        self.fail('runner job did not reach expected state')

    def test_command_idempotency_output_and_owner_scope(self):
        args = {'cwd': str(self.root), 'command': 'printf hello', 'idempotency_key': 'one'}
        first = self.call('command.start', args)['result']
        second = self.call('command.start', args)['result']
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(self.wait(first['id'])['output'], 'hello')
        self.assertFalse(self.call('command.start', dict(args, command='false'))['ok'])
        self.assertFalse(self.call('terminal.poll', {'id': first['id']}, owner='bob')['ok'])
        self.assertFalse(self.call('terminal.poll', {'id': first['id']}, scope='other')['ok'])
        self.assertNotIn('printf hello', (self.root / 'state/metadata.json').read_text())

    def test_isolated_command_never_accepts_a_user_workspace_or_docker_flags(self):
        # Exercise the policy boundary before the host-specific Docker gate.
        self.runner.platform_identity['os'] = 'linux'
        rejected = self.call('sandbox.command.start', {
            'cwd': str(self.root), 'command': 'true', 'idempotency_key': 'isolated-root'})
        self.assertFalse(rejected['ok'])
        self.assertIn('verification copy', rejected['error'])
        rejected = self.call('sandbox.command.start', {
            'cwd': str(self.root), 'command': 'true', 'idempotency_key': 'isolated-flags',
            'image': 'attacker/image:latest'})
        self.assertFalse(rejected['ok'])
        self.assertIn('only fixed check arguments', rejected['error'])
        self.assertIn('sandbox.command.start', self.call('runner.capabilities')['result']['supported_ops'])

    def test_isolated_command_requires_separate_hashed_heldout_mount(self):
        project = self.root / 'project'
        heldout = self.root / 'heldout'
        project.mkdir(); heldout.mkdir()
        (project / 'code.py').write_text('value = 1\n')
        (heldout / 'cases.json').write_text('{}\n')
        self.runner.platform_identity['os'] = 'linux'
        digest = self.call('workspace.digest', {'cwd': str(project)})['result']['sha256']
        copied = self.call('workspace.verification-copy', {
            'source': str(project), 'expected_source_sha256': digest,
            'idempotency_key': 'isolated-copy',
        })['result']
        with patch.object(self.module.shutil, 'which', return_value='/usr/bin/docker'):
            rejected = self.call('sandbox.command.start', {
                'cwd': copied['path'], 'command': 'true', 'idempotency_key': 'missing-heldout',
                'expected_workspace_hash': digest, 'check_run_id': 'a' * 32,
            })
        self.assertFalse(rejected['ok'])
        self.assertIn('sealed environment', rejected['error'])
        heldout_digest = self.call('workspace.digest', {'cwd': str(heldout)})['result']['sha256']
        (heldout / 'cases.json').write_text('{"changed":true}\n')
        with patch.object(self.module.shutil, 'which', return_value='/usr/bin/docker'):
            rejected = self.call('sandbox.command.start', {
                'cwd': copied['path'], 'command': 'true', 'idempotency_key': 'stale-heldout',
                'expected_workspace_hash': digest, 'check_run_id': 'b' * 32,
                'sealed_environment': str(heldout),
                'expected_environment_hash': heldout_digest,
            })
        self.assertFalse(rejected['ok'])
        self.assertIn('changed before check dispatch', rejected['error'])

    def test_workspace_git_state_is_fixed_clean_head(self):
        project = self.root / 'git-project'
        subprocess.run(['git', 'init', '-q', str(project)], check=True)
        (project / 'file.txt').write_text('one\n')
        subprocess.run(['git', '-C', str(project), 'add', 'file.txt'], check=True)
        subprocess.run(['git', '-C', str(project), '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'one'], check=True)
        state = self.call('workspace.git-state', {'cwd': str(project)})['result']
        self.assertTrue(state['clean'])
        self.assertRegex(state['head'], r'^[0-9a-f]{40}$')
        (project / 'file.txt').write_text('dirty\n')
        self.assertFalse(self.call('workspace.git-state', {'cwd': str(project)})['result']['clean'])

    def test_pty_input_resize_interrupt_and_stop(self):
        terminal = self.call('terminal.create', {'cwd': str(self.root), 'idempotency_key': 'pty'})['result']
        identity = terminal['id']
        self.assertTrue(self.call('terminal.resize', {'id': identity, 'rows': 42, 'cols': 111})['ok'])
        self.assertTrue(self.call('terminal.input', {'id': identity, 'data': 'printf "PTY_OK\\n"; stty size\n'})['ok'])
        result = self.wait(identity, lambda r: '42 111' in r['output'])
        self.assertIn('PTY_OK', result['output'])
        self.call('terminal.input', {'id': identity, 'data': 'sleep 30\n'})
        time.sleep(.1)
        self.assertTrue(self.call('terminal.interrupt', {'id': identity})['ok'])
        self.call('terminal.input', {'id': identity, 'data': 'echo AFTER_INTERRUPT\n'})
        self.wait(identity, lambda r: 'AFTER_INTERRUPT' in r['output'])
        self.assertTrue(self.call('terminal.stop', {'id': identity})['ok'])
        self.assertNotEqual(self.wait(identity)['exit_code'], 0)

    def test_timeout_and_ring_offset(self):
        identity = self.call('command.start', {'cwd': str(self.root), 'command': 'sleep 30', 'timeout': 1, 'idempotency_key': 'timeout'})['result']['id']
        self.assertEqual(self.wait(identity)['status'], 'timed_out')
        command = f'{sys.executable} -c "print(\"x\" * 1200000)"'
        # Shell quoting independent of the helper's implementation.
        import shlex
        command = shlex.join([sys.executable, '-c', 'print("x" * 1200000)'])
        identity = self.call('command.start', {'cwd': str(self.root), 'command': command, 'idempotency_key': 'ring'})['result']['id']
        output = self.wait(identity)
        self.assertTrue(output['truncated'])
        self.assertGreater(output['output_start'], 0)
        self.assertLessEqual(len((self.root / ('state/' + identity + '.output')).read_bytes()), self.module.MAX_OUTPUT)
        self.assertEqual(len(base64.b64decode(output['output_base64'])), output['next_offset'] - output['offset'])

    def test_restart_marks_interrupted_never_replays(self):
        record = {'id': 'lost', 'owner': 'alice', 'scope': 'task', 'key': 'lost',
                  'signature': 'x', 'kind': 'command.start', 'cwd': str(self.root),
                  'status': 'running', 'output_start': 0, 'output_end': 0}
        self.runner.data['jobs']['lost'] = record
        self.runner.save()
        restarted = self.module.Runner(self.root / 'state')
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.data['jobs']['lost']['status'], 'interrupted')
        self.assertEqual(restarted.processes, {})

    def test_binary_roundtrip_cas_and_special_file(self):
        args = {'cwd': str(self.root), 'path': 'blob', 'data_base64': base64.b64encode(b'\x00\xffbinary').decode()}
        result = self.call('file.upload', args)
        self.assertTrue(result['ok'], result)
        self.assertEqual(base64.b64decode(self.call('file.download', {'cwd': str(self.root), 'path': 'blob'})['result']['data_base64']), b'\x00\xffbinary')
        self.assertFalse(self.call('file.upload', args)['ok'])
        self.assertTrue(self.call('file.upload', dict(args, expected_sha256=result['result']['sha256']))['ok'])
        self.assertFalse(self.call('file.download', {'cwd': str(self.root), 'path': '/dev/zero'})['ok'])

    def test_resource_snapshot_and_structured_listing(self):
        (self.root / 'file.txt').write_text('hello')
        (self.root / 'directory').mkdir()
        result = self.call('file.call', {'tool': 'ls', 'content': {'path': str(self.root)}, 'cwd': str(self.root)})
        self.assertTrue(result['ok'], result)
        entries = {entry['name']: entry for entry in result['result']['entries']}
        self.assertEqual(entries['file.txt']['path'], str(self.root / 'file.txt'))
        self.assertFalse(entries['file.txt']['is_dir'])
        self.assertTrue(entries['directory']['is_dir'])
        snapshot = self.call('resource.snapshot')['result']
        self.assertIn('cpu_percent', snapshot)
        self.assertIn('memory', snapshot)
        self.assertIsInstance(snapshot['temperatures'], list)

    def test_model_policy_blocks_credentials_nested_search_and_outside_write(self):
        workspace = self.root / 'workspace'
        workspace.mkdir()
        (workspace / 'ordinary.txt').write_text('needle ordinary')
        (workspace / '.ssh').mkdir()
        (workspace / '.ssh/key').write_text('needle SECRET')
        (workspace / '.env').write_text('needle TOKEN')
        policy = {'cwd': str(workspace), 'write_scope': None}
        def file_call(tool, content, guarded=True):
            args = {'cwd': str(workspace), 'tool': tool, 'content': content}
            if guarded:
                args['model_policy'] = policy
            return self.call('file.call', args)
        search = file_call('grep', {'pattern': 'needle'})
        self.assertTrue(search['ok'], search)
        self.assertEqual(search['result']['exit_code'], 0, search)
        self.assertIn('needle ordinary', search['result']['output'])
        self.assertNotIn('SECRET', search['result']['output'])
        self.assertNotIn('TOKEN', search['result']['output'])
        listing = file_call('ls', {})['result']
        self.assertNotIn('.ssh', [row['name'] for row in listing['entries']])
        self.assertFalse(file_call('read_file', {'path': '.ssh/key'})['ok'])
        self.assertFalse(file_call('write_file', {'path': '../outside', 'content': 'no'})['ok'])
        self.assertFalse((self.root / 'outside').exists())
        self.assertIn('SECRET', file_call('read_file', {'path': '.ssh/key'}, guarded=False)['result']['output'])
        (workspace / 'link').symlink_to(self.root, target_is_directory=True)
        self.assertFalse(file_call('write_file', {'path': 'link/outside', 'content': 'no'})['ok'])

    def test_scope_cancel_fences_late_creation_and_preserves_other_scopes(self):
        args = {'cwd': str(self.root), 'command': 'sleep 30', 'idempotency_key': 'running'}
        identity = self.call('command.start', args)['result']['id']
        cancelled = self.call('scope.cancel')
        self.assertTrue(cancelled['ok'])
        self.assertIn(identity, cancelled['result']['job_ids'])
        self.assertFalse(self.call('command.start', dict(args, idempotency_key='late'))['ok'])
        self.assertFalse(self.call('terminal.create', {'cwd': str(self.root), 'idempotency_key': 'late-pty'})['ok'])
        self.assertTrue(self.call('command.start', dict(args, command='true'), scope='other')['ok'])
        self.wait(identity)
        self.runner.close()
        restarted = self.module.Runner(self.root / 'state')
        self.addCleanup(restarted.close)
        self.assertFalse(restarted.handle({'op': 'command.start', 'owner': 'alice', 'scope': 'task', 'args': args})['ok'])

    def test_output_retention_preserves_idempotency_tombstone(self):
        with patch.object(self.module, 'MAX_OUTPUT_JOBS', 2):
            first_args = {'cwd': str(self.root), 'command': 'printf x >> counter; printf first', 'idempotency_key': 'first'}
            identity = self.call('command.start', first_args)['result']['id']
            self.wait(identity)
            for number in range(2):
                other = self.call('command.start', {'cwd': str(self.root), 'command': 'printf later', 'idempotency_key': str(number)})['result']['id']
                self.wait(other)
            polled = self.call('terminal.poll', {'id': identity})['result']
            self.assertTrue(polled['output_pruned'])
            self.assertIn('will not be replayed', polled['notice'])
            recovered = self.call('command.start', first_args)['result']
            self.assertEqual(recovered['id'], identity)
            self.assertEqual((self.root / 'counter').read_text(), 'x')
            self.assertEqual(len(self.runner.data['jobs']), 3)
            self.assertLessEqual(len(list((self.root / 'state').glob('*.output'))), 2)

    def test_socket_client_disconnect_does_not_stop_job(self):
        state = self.root / 'socket-state'
        script = Path(__file__).resolve().parents[1] / 'scripts/host_runner.py'
        process = subprocess.Popen([sys.executable, str(script)], env=dict(os.environ, ODYSSEUS_HOST_RUNNER_STATE=str(state)), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while not (state / 'runner.sock').exists() and time.monotonic() < deadline:
                time.sleep(.03)
            self.assertEqual((state / 'runner.sock').stat().st_mode & 0o777, 0o600)
            request = {'owner': 'alice', 'scope': 'task', 'op': 'command.start',
                       'args': {'cwd': str(self.root), 'command': 'sleep .2; printf durable', 'idempotency_key': 'disconnect'}}
            with socket.socket(socket.AF_UNIX) as connection:
                connection.connect(str(state / 'runner.sock'))
                connection.sendall(json.dumps(request).encode() + b'\n')
                # Deliberately do not consume the response.
            time.sleep(.4)
            import host_runner_client
            recovered = host_runner_client.call(request, state)['result']
            output = host_runner_client.call({'owner': 'alice', 'scope': 'task', 'op': 'terminal.poll', 'args': {'id': recovered['id']}}, state)
            self.assertEqual(output['result']['output'], 'durable')
        finally:
            process.terminate()
            process.wait(timeout=6)


if __name__ == '__main__':
    unittest.main()
