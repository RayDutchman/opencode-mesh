"""真实控制终端验证交互取消发生在服务变更之前。"""
import os
import pty
import select
import signal
import time
from pathlib import Path

import pytest


@pytest.mark.parametrize('script,answers', [
    ('upgrade.sh', [('Target host', ''), ('Install directory', '/tmp/mesh-acceptance'), ('Role', 'agent'), ('Scope', 'user'), ('Git ref', 'HEAD'), ('Continue', 'n')]),
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
