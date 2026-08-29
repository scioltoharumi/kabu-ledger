"""YAML の読み込みと、**master.yaml の共通語彙**を1箇所に集める。

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


# =============================================================================
# 監視対象フラグ（master.yaml の `watch`）
# =============================================================================
#
# なぜここに置くか:
#   取得・判定・生成・検査がそれぞれ master.yaml を自前で読んでいる。
#   フィルタを各所に書くと**必ずどこか1つ忘れる**。忘れた先で起きるのは
#   「対象外にしたはずの銘柄を取りに行く」か、もっと悪い
#   「対象外の銘柄が判定に混ざる」。語彙と判定を1箇所に置いて全員に使わせる。
#
# 固定語彙。`holding.status` と同じ規律で、**語彙外は安全側に倒す**。
# ここでの安全側は `active`（監視を続ける）。理由は、綴り間違いで銘柄が
# **黙って台帳から消える**方が、余計に取得してしまうより明確に悪いから。
WATCH_ACTIVE = "active"
WATCH_EXCLUDED = "excluded"
WATCH_VOCAB = (WATCH_ACTIVE, WATCH_EXCLUDED)


def watch_state(stock: Any) -> str:
    """その銘柄の監視状態。**未記載・語彙外は `active`**（安全側）。"""
    if not isinstance(stock, dict):
        return WATCH_ACTIVE
    v = str(stock.get("watch", "") or "").strip().lower()
    return WATCH_EXCLUDED if v == WATCH_EXCLUDED else WATCH_ACTIVE


def is_watched(stock: Any) -> bool:
    """取得・判定・週次追記の対象か。"""
    return watch_state(stock) == WATCH_ACTIVE


def watched_stocks(master: Any) -> list:
    """master.yaml の `stocks` のうち監視対象だけ。**取得と判定はこれを使う。**

    `master["stocks"]` を直接回してよいのは、対象外も含めて全部を扱う場所
    （build.py の一覧表示・checks.py の追記性検査）だけ。
    """
    if not isinstance(master, dict):
        return []
    return [s for s in (master.get("stocks") or []) if is_watched(s)]


def excluded_codes(master: Any) -> set:
    """対象外の銘柄コード（表示と検査の除外に使う）。"""
    if not isinstance(master, dict):
        return set()
    return {str(s.get("code")) for s in (master.get("stocks") or [])
            if not is_watched(s)}
