import shutil
import subprocess
from pathlib import Path

import pytest


def test_checkout_administrator_and_student_clients():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for checkout administrator client checks')
    subprocess.run([node, '--test', 'tests/checkout_admin.test.cjs', 'tests/account_checkout.test.cjs'],
                   cwd=Path(__file__).resolve().parents[1], check=True, timeout=30)
