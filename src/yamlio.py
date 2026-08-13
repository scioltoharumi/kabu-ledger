"""YAML の読み込みを1箇所に集める（libyaml があれば C 実装を使う）。

なぜ要るか
----------
`yaml.safe_load` は pure-Python の SafeLoader を使う。実測では
`run_checks` 1回の cumtime の **45%**（0.937s / 2.061s）が YAML の構文解析で、
libyaml (CSafeLoader) に替えるだけで:
    tests 全数（CI と同じ直列）  100.8s → 49.2s
    run_checks ウォーム            424ms → 270ms
    yaml.safe_load(master+sources)  74ms →   9ms
libyaml が入っていない環境では純 Python にフォールバックする（挙動は同じ・遅いだけ）。
黙って遅い経路に落ちると「速くしたつもり」に気づけないので、`checks.py` の
実行末尾で USING_LIBYAML を見て WARN を1行出す。
"""
from __future__ import annotations

from typing import Any

import yaml

try:                        # libyaml 付きでビルドされた pyyaml なら C 実装
    from yaml import CSafeLoader as _SafeLoader
except ImportError:         # 無ければ純 Python（速度以外は同じ）
    _SafeLoader = yaml.SafeLoader

USING_LIBYAML = _SafeLoader is not yaml.SafeLoader


def safe_load(stream: Any) -> Any:
    """`yaml.safe_load` と同じ。投げる例外も yaml.YAMLError のまま。"""
    return yaml.load(stream, Loader=_SafeLoader)
