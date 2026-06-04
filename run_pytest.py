import subprocess
import os
import sys

try:
    result = subprocess.check_output(["pytest", "tests/"], cwd="/app/app", stderr=subprocess.STDOUT)
    with open('/tmp/pytest_out.txt', 'wb') as f:
        f.write(result)
except subprocess.CalledProcessError as e:
    with open('/tmp/pytest_out.txt', 'wb') as f:
        f.write(e.output)
