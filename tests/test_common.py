from __future__ import annotations

import re

from dedup_photos.common import default_log_path


def test_default_log_path_is_prefixed_with_action(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    path = default_log_path("execute_plan")

    assert path.parent == tmp_path
    assert re.fullmatch(r"execute_plan_\d{8}_\d{6}\.csv", path.name)
