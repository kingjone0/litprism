"""A fixed reading run must not gain extra tasks, source drift or blind retries."""
import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / 'tools/short_reading_acceptance.py'
spec = importlib.util.spec_from_file_location('short_reading_tested', PATH)
reading = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reading)


@pytest.mark.parametrize('task,sent,body', [
    ('semantic_review', 0, 'source'), ('concepts', 0, 'source'),
    ('extract', 3, 'source'), ('extract', 0, 'different source')])
def test_dispatch_rejects_extra_tasks_calls_and_prefix_changes(task, sent, body):
    from litprism.reading import TASK_SEPARATOR
    messages = [{'role': 'user', 'content': body + TASK_SEPARATOR + 'instruction'}]
    with pytest.raises(ValueError):
        reading.validate_dispatch(task, messages, 'source', sent, 3)


def test_native_repair_may_change_instruction_but_not_paper_prefix():
    from litprism.reading import TASK_SEPARATOR
    for sent, instruction in enumerate(['initial directive', 'bounded repair', 'last native repair']):
        reading.validate_dispatch('extract', [{'content': 'source' + TASK_SEPARATOR + instruction}],
                                  'source', sent, 3)


def test_started_run_is_not_resent_and_does_not_read_credentials(tmp_path, monkeypatch):
    reading.write(tmp_path / 'manifest.json', {'frozen': True})
    reading.write(tmp_path / 'started.json', {'never_repeat': True})
    monkeypatch.setattr(reading, 'isolated', lambda p: p.resolve())
    monkeypatch.setattr(reading, 'verify', lambda p: {})

    def unexpected(*args, **kwargs):
        raise AssertionError('Repeated run touched credential storage')

    monkeypatch.setattr(reading.sqlite3, 'connect', unexpected)
    with pytest.raises(FileExistsError):
        reading.live(tmp_path, tmp_path / 'credentials')
