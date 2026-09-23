import atexit
import os
import shutil
import tempfile

# 任务日志文件写到临时目录，测试不往项目 logs/ 里留东西。
_TASK_LOG_ROOT = tempfile.mkdtemp(prefix="grok-register-test-logs-")
os.environ["GROK_LOG_DIR"] = _TASK_LOG_ROOT
atexit.register(shutil.rmtree, _TASK_LOG_ROOT, True)
