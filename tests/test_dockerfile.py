"""Dockerfile：第三方套件自成一層（只有 pyproject.toml 改了才重裝），裝的清單要跟 pyproject 一致。"""

import re
import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


def _steps() -> list[str]:
    """Dockerfile 的指令（續行併成一行，略過註解）。"""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8").replace("\\\n", " ")
    return [s.strip() for s in text.splitlines() if s.strip() and not s.strip().startswith("#")]


def test_dependency_layer_installs_exactly_pyproject_dependencies():
    steps = _steps()
    dep = next(i for i, s in enumerate(steps) if "tomllib" in s)
    # 照 Dockerfile 那一行的 python 程式實際跑一次（建置時在 /app，與 pyproject.toml 同一層）
    code = re.search(r'python -c "([^"]+)"', steps[dep]).group(1)
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    want = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    assert out.splitlines() == want
    for r in want:
        Requirement(r)                                     # pip 讀得懂（requirements 檔一行一個）
    assert "pip install --no-cache-dir -r /tmp/requirements.txt" in steps[dep] and ">/tmp/requirements.txt" in steps[dep]

    # 順序：先只複製 pyproject.toml 裝套件，之後才複製程式、只裝本系統（--no-deps，不再碰第三方套件）
    order = [steps.index("COPY pyproject.toml ./"), dep, steps.index("COPY src ./src"),
             steps.index("RUN pip install --no-cache-dir --no-deps .")]
    assert order == sorted(order) and order[1] == order[0] + 1
    assert not any(re.search(r"pip install (--no-cache-dir )?\.$", s) for s in steps)   # 沒有整包重裝的舊寫法
    # 資料、字型快取、執行身分照舊
    for s in ["COPY data/lawdb ./data/lawdb", "COPY data/tables ./data/tables", "COPY data/review ./data/review",
              "USER 65534:65534"]:
        assert s in steps, s
    assert steps[-1].startswith('CMD ["uvicorn", "litian.api:app"')
