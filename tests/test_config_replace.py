"""A transient Windows sharing error must not turn a valid settings save into data loss."""
from pathlib import Path

import pytest
import yaml

from litprism.config import Config, config_to_dict, patch_config


@pytest.mark.parametrize('transient', [True, False])
def test_windows_replace_failure_is_bounded_and_keeps_old_file(tmp_path, monkeypatch, transient):
    target = tmp_path / 'config.yaml'
    before = yaml.safe_dump(config_to_dict(Config()))
    target.write_text(before, encoding='utf-8')
    original = Path.replace
    attempts = []

    def replace(path, destination):
        attempts.append(str(destination))
        if not transient or len(attempts) == 1:
            failure = PermissionError('Controlled Windows sharing error')
            failure.winerror = 5
            raise failure
        return original(path, destination)

    monkeypatch.setattr(Path, 'replace', replace)
    if transient:
        patch_config(target, {'retrieval': {'top_k': 7}})
        assert yaml.safe_load(target.read_text(encoding='utf-8'))['retrieval']['top_k'] == 7
        assert len(attempts) == 2
    else:
        with pytest.raises(PermissionError):
            patch_config(target, {'retrieval': {'top_k': 7}})
        assert len(attempts) == 4 and target.read_text(encoding='utf-8') == before
