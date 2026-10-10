import os
import tempfile

# Тесты не должны дописывать настоящий журнал планов; переменную наследует и процесс стратегии.
os.environ.setdefault("BATTLESHIPS_PLAN_LOG", os.path.join(tempfile.mkdtemp(), "plans.jsonl"))
