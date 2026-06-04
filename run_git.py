import subprocess
import os

with open('/tmp/git_diff.txt', 'w') as f:
    f.write(subprocess.check_output(["git", "diff"], cwd="/app").decode("utf-8"))

with open('/tmp/git_status.txt', 'w') as f:
    f.write(subprocess.check_output(["git", "status"], cwd="/app").decode("utf-8"))
