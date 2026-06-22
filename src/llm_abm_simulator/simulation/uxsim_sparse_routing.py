"""uxsim_sparse_routing.py — 稀疏終點 route_search（UXsim DUO 選路的吞吐優化）。

## 問題
UXsim 的 `RouteChoice.route_search_all` 每 `duo_update_time` 對**全節點當終點**跑一次
scipy all-pairs Dijkstra（N=路網節點數，台南全市約 1.5 萬）：複雜度 O(N·(E+N·logN))、單核、
N² 記憶體。城市尺度單次重算可達數百秒（server 實測 step1 move≈340s），是吞吐瓶頸。

## 觀察
我們其實只需要「**有車真的要去的那些終點**」的最短路樹——其他節點當終點算了也沒車用。
搭配 `[demand].dest_pool_per_capita`（每區只取 ceil(人口/N) 個終點），全市不同終點數從上萬→數百千個。

## 做法（結果完全一致、只算得少）
把 `route_search_all` 與 `homogeneous_DUO_update` 換成「只對 active 終點集合 D」的版本：
- `route_search_all`：建同一個 `adj_mat_time`（**逐 link 逐字複製原版**，含 noise/多 link 平均 →
  rng 消耗與結果與原版位元一致），再用 `dijkstra(adj.T, indices=D)` 只從 D 個源算（= 到 D 的最短路）。
- `homogeneous_DUO_update`：只更新 `route_pref[k]`（k∈D），其餘列維持 0（沒車去、不影響）。

## 為何安全
`RouteChoice.next`/`dist` 在 UXsim 內**只被 `homogeneous_DUO_update` 消費**（per-vehicle 的
`route_pref_update` 只在 `heterogeneous_DUO` 分支用 `next`，本專案不用；homogeneous_DUO 車讀
`route_pref[dest.id]`；`fixed`（tolerate）車 `route_pref_update` 直接 no-op）。故可完全 compact、
不配 N×N。`dist_record` 在原版為唯寫（不被讀）→ 省略。

對拍：相同 `adj_mat_time` + 相同 dijkstra → `route_pref[D]` 與原版逐元素相同（見 tests/test_sparse_routing.py）。
以 `[uxsim].sparse_route_search=false` 可關閉、退回 UXsim 原生 all-pairs（除錯/對拍）。

## 已知限制（可接受、可自癒）
D 取自「目前所有 Vehicle 的終點」。若某終點節點在某次 route_search 當下**沒有任何車以它為終點**
（例如 respawn/egress 才首次用到的池節點），該終點的 route_pref 要等**下一次** route_search 才建好，
中間該車可能短暫亂走。實務上終點都來自有界終點池、車數遠多於池節點 → 幾乎每個池節點時時都有車要去，
此情形極少且會在下次重算自癒。原生 all-pairs 無此延遲（每次都把所有節點當終點算）。
"""

from __future__ import annotations

import logging

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

logger = logging.getLogger(__name__)


def _active_dest_ids(W) -> np.ndarray:
    """目前所有 vehicle（含未出發/已抵達）的終點節點 id 集合（排序、去重）。"""
    ids = set()
    for v in W.VEHICLES.values():
        d = getattr(v, "dest", None)
        if d is not None:
            ids.add(d.id)
    return np.array(sorted(ids), dtype=int)


def _build_adj_mat_time(s, t: float, noise: float) -> None:
    """逐 link 建 adj_mat_time——**與 uxsim.RouteChoice.route_search_all 完全相同**（保證結果一致）。"""
    W = s.W
    n = len(W.NODES)
    s.adj_mat_time = np.zeros([n, n])
    adj_mat_link_count = np.zeros([n, n])
    for link in W.LINKS:
        i = link.start_node.id
        j = link.end_node.id
        if W.ADJ_MAT[i, j]:
            if W.hard_deterministic_mode is False:
                new_link_tt = link.traveltime_instant[-1] * W.rng.uniform(1, 1 + noise) + link.get_toll(t)
            else:
                new_link_tt = link.traveltime_instant[-1] + link.get_toll(t)
            cnt = adj_mat_link_count[i, j]
            s.adj_mat_time[i, j] = s.adj_mat_time[i, j] * cnt / (cnt + 1) + new_link_tt / (cnt + 1)
            adj_mat_link_count[i, j] += 1
            if link.capacity_in == 0:
                s.adj_mat_time[i, j] = np.inf
        else:
            s.adj_mat_time[i, j] = np.inf


def _sparse_route_search_all(s, t: float, infty=np.inf, noise=0):
    """只對 active 終點 D 算最短路樹（取代 all-pairs）。把 next 存成 compact (|D|, N)。"""
    _build_adj_mat_time(s, t, noise)
    D = _active_dest_ids(s.W)
    s._sparse_dests = D
    if len(D) == 0:
        s._sparse_next = None
        return
    # 在轉置圖上從 D 個終點各做一次 Dijkstra：pred[m, i] = 在原圖中「從 i 往終點 D[m] 的下一個節點」。
    _dist, pred = dijkstra(csr_matrix(s.adj_mat_time).T, indices=D, return_predecessors=True)
    s._sparse_next = pred   # shape (|D|, N)


def _sparse_homogeneous_DUO_update(s):
    """只更新 route_pref 的 active 終點列（k∈D）；其餘列維持 0（沒車去、不影響選路）。"""
    W = s.W
    D = getattr(s, "_sparse_dests", None)
    nxt = getattr(s, "_sparse_next", None)
    if D is None or nxt is None or len(D) == 0:
        return
    if W.route_choice_update_gradual:
        weight0 = W.DUO_UPDATE_WEIGHT * (W.DELTAT / W.DUO_UPDATE_TIME)
    else:
        weight0 = W.DUO_UPDATE_WEIGHT
    start_nodes = np.array([l.start_node.id for l in W.LINKS])
    end_nodes = np.array([l.end_node.id for l in W.LINKS])
    rp = s.route_pref
    for m in range(len(D)):
        k = int(D[m])
        # 該終點下，每條 link 是否為「其起點往 k 的下一跳」→ 1，其餘 0（與原版 next_node_mask 同義）。
        mask = end_nodes == nxt[m][start_nodes]
        weight = 1.0 if np.sum(rp[k]) == 0 else weight0   # 空 preference → 確定性初始化（同原版）
        rp[k] = rp[k] * (1 - weight) + weight * mask


# UXsim 在 exec_simulation 內才建 W.ROUTECHOICE（非 World() 當下）→ 改 patch RouteChoice **類別**
# （時機無關、所有後續/既有 World 一致）。存原版以便 disable 還原（對拍/baseline）。
_PATCHED = False
_ORIG: dict = {}


def enable_sparse_routing() -> None:
    """patch uxsim.RouteChoice 類別 → DUO 用稀疏版（process-wide、idempotent）。"""
    global _PATCHED
    if _PATCHED:
        return
    from uxsim.uxsim import RouteChoice
    _ORIG["route_search_all"] = RouteChoice.route_search_all
    _ORIG["homogeneous_DUO_update"] = RouteChoice.homogeneous_DUO_update
    RouteChoice.route_search_all = _sparse_route_search_all
    RouteChoice.homogeneous_DUO_update = _sparse_homogeneous_DUO_update
    _PATCHED = True
    logger.info("UXsim 稀疏 route_search 已啟用（DUO 只對實際終點算最短路）")


def disable_sparse_routing() -> None:
    """還原 UXsim 原生 all-pairs route_search（[uxsim].sparse_route_search=false / 對拍 / 測試）。"""
    global _PATCHED
    if not _PATCHED:
        return
    from uxsim.uxsim import RouteChoice
    RouteChoice.route_search_all = _ORIG["route_search_all"]
    RouteChoice.homogeneous_DUO_update = _ORIG["homogeneous_DUO_update"]
    _PATCHED = False
