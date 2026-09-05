import json
from secrets import token_urlsafe
import subprocess
import sys

import pytest

from workbench.credentials.service import VaultService


READER = '''
import json,sys
from pathlib import Path
from workbench.credentials.service import VaultService
from workbench.credentials.models import VaultUnlockError,VaultInUseError
value=json.load(sys.stdin)
vault=VaultService(Path(sys.argv[1]),read_only=value['read_only'])
try:
    vault.unlock(value['password'])
    assert vault.get('test')==value['expected']
    print('read_ok')
except VaultUnlockError:
    print('wrong_password')
except VaultInUseError:
    print('writer_conflict')
finally:
    vault.lock()
'''


def test_readonly_process_can_read_while_writer_is_unlocked(tmp_path):
    password, secret = token_urlsafe(24), token_urlsafe(24)
    path = tmp_path / "credentials.vault"
    writer = VaultService(path)
    writer.create(password)
    writer.put("test", secret)
    original = path.read_bytes()
    try:
        for supplied, read_only, expected in ((password, False, "writer_conflict"),
                (password, True, "read_ok"), (token_urlsafe(24), True, "wrong_password")):
            result = subprocess.run([sys.executable, "-c", READER, str(path)],
                input=json.dumps({"password": supplied, "expected": secret, "read_only": read_only}),
                text=True, capture_output=True, timeout=15)
            assert result.returncode == 0, result.stderr
            assert result.stdout.strip() == expected
            assert path.read_bytes() == original
        assert writer.status == "unlocked"
        writer.put("second", token_urlsafe(20))
    finally:
        writer.lock()


def test_readonly_service_cannot_mutate_or_create(tmp_path):
    password = token_urlsafe(24)
    path = tmp_path / "credentials.vault"
    writer = VaultService(path)
    writer.create(password)
    writer.lock()
    reader = VaultService(path, read_only=True)
    reader.unlock(password)
    original = path.read_bytes()
    for action in (lambda: reader.put("a", "b"), lambda: reader.delete("a"),
                   lambda: reader.create(password), lambda: reader.recover(password),
                   lambda: VaultService(tmp_path / "missing", read_only=True).create(password)):
        with pytest.raises(PermissionError):
            action()
    assert path.read_bytes() == original
    reader.lock()
