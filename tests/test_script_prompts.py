"""真实控制终端验证交互取消发生在服务变更之前。"""
import os
import pty
import select
import signal
import time
from pathlib import Path

import pytest


@pytest.mark.parametrize('script,answers', [
    ('uninstall.sh', [('Uninstall mode', 'agent'), ('Agent instance', 'acceptance'), ('Continue', 'n')]),
])
def test_interactive_cancel(script, answers):
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp('bash', ['bash', str(Path(__file__).parents[1] / 'scripts' / script)])
    try:
        pending = b''
        for prompt, answer in answers:
            deadline = time.monotonic() + 5
            while prompt.encode() not in pending:
                assert time.monotonic() < deadline, (prompt, pending)
                if select.select([fd], [], [], .1)[0]:
                    try:
                        block = os.read(fd, 65536)
                    except OSError:
                        block = b''
                    assert block, (prompt, pending)
                    pending += block
            pending = b''
            os.write(fd, (answer + '\n').encode())
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        os.close(fd)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.parametrize('multiple', [False, True])
def test_upgrade_auto_discovery_cancel(tmp_path, monkeypatch, multiple):
    folder = tmp_path / 'installed mesh'
    folder.mkdir()
    (folder / '.mesh-revision').write_text('old-revision')
    binary = tmp_path / 'systemctl'
    binary.write_text('''#!/usr/bin/env python3
import sys
a=sys.argv[1:]
if '--user' not in a: sys.exit(0)
if 'list-unit-files' in a or 'list-units' in a:
 print('opencode-mesh-agent.service enabled')
 if MULTIPLE: print('opencode-mesh-agent@second.service enabled')
elif 'show' in a:
 print(ROOT + ('-second' if '@second' in ' '.join(a) else ''))
else: sys.exit(97)
'''.replace('MULTIPLE', repr(multiple)).replace('ROOT', repr(str(folder))))
    binary.chmod(0o755)
    if multiple: Path(str(folder)+'-second').mkdir()
    for location in [folder, Path(str(folder)+'-second')] if multiple else [folder]:
        (location / '.venv/bin').mkdir(parents=True)
        (location / '.venv/bin/python').symlink_to('/bin/true')
        (location / 'src').mkdir()
        (location / 'scripts').mkdir()
        (location / 'pyproject.toml').write_text('')
    monkeypatch.setenv('PATH', str(tmp_path)+os.pathsep+os.environ['PATH'])
    monkeypatch.chdir(tmp_path)
    answers = [('请选择', 'bad'), ('请选择', '2')] if multiple else []
    test_interactive_cancel('upgrade.sh', answers+[('是否升级', 'n')])


@pytest.mark.parametrize('scope,ref,message', [
    ('opencode', 'HEAD', '不是用户名'),
    ('user', 'missing-reference-for-test', '找不到 Git 引用'),
])
def test_invalid_arguments_explain_error(scope, ref, message):
    import subprocess
    result = subprocess.run(['bash', str(Path(__file__).parents[1]/'scripts/upgrade.sh'),
        '127.0.0.1', '~/.local/share/mesh-test', 'agent', scope, ref], capture_output=True, text=True)
    assert result.returncode == 2
    assert message in result.stderr
