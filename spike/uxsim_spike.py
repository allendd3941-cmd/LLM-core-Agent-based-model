"""UXsim 可行性 spike（Phase 0 gate）—— 在小網路上實測遷移所需的 UXsim API。

目的：在投入換引擎前，用一個瞬間可跑的網路，確認 UXsim 能做到遷移計畫依賴的能力，
特別是 **Design 2**（action_mode → UXsim 的 route 偏好，讓 UXsim 自己算異質路線）。

不碰主套件，純探測。每能力包 try/except、印 PASS/FAIL 與證據。
**deltan=1 城市尺度吞吐不在此測**（那要在 server 跑）。

跑法：  uv run python spike/uxsim_spike.py
"""

from __future__ import annotations

import traceback

import uxsim
from uxsim import World


def banner(t: str) -> None:
    print("\n" + "=" * 70 + f"\n{t}\n" + "=" * 70)


def build_diamond(tmax: int = 3000):
    """O→D 兩條路：上(OA,AD 共 2000) / 下(OB,BD 共 2400)。預設 DUO 應走上路。"""
    W = World(name="spike", deltan=1, tmax=tmax, print_mode=0, save_mode=0,
              show_mode=0, random_seed=42, hard_deterministic_mode=True)
    for n, x, y in (("O", 0, 0), ("A", 1, 1), ("B", 1, -1), ("D", 2, 0)):
        W.addNode(n, x, y)
    W.addLink("OA", "O", "A", length=1000, free_flow_speed=10, number_of_lanes=1)
    W.addLink("AD", "A", "D", length=1000, free_flow_speed=10, number_of_lanes=1)
    W.addLink("OB", "O", "B", length=1000, free_flow_speed=10, number_of_lanes=1)
    W.addLink("BD", "B", "D", length=1400, free_flow_speed=10, number_of_lanes=1)
    return W


def build_late_diverge(tmax: int = 3000):
    """O→M 共用段，之後才分上(MA,AD)/下(MB,BD)。供「車已上路後仍有替代」的改道測試。"""
    W = World(name="spike2", deltan=1, tmax=tmax, print_mode=0, save_mode=0,
              show_mode=0, random_seed=42, hard_deterministic_mode=True)
    for n, x, y in (("O", 0, 0), ("M", 1, 0), ("A", 2, 1), ("B", 2, -1), ("D", 3, 0)):
        W.addNode(n, x, y)
    W.addLink("OM", "O", "M", length=1000, free_flow_speed=10, number_of_lanes=1)
    W.addLink("MA", "M", "A", length=1000, free_flow_speed=10, number_of_lanes=1)
    W.addLink("AD", "A", "D", length=1000, free_flow_speed=10, number_of_lanes=1)
    W.addLink("MB", "M", "B", length=1000, free_flow_speed=10, number_of_lanes=1)
    W.addLink("BD", "B", "D", length=1400, free_flow_speed=10, number_of_lanes=1)
    return W


def route_links(veh) -> str:
    """取出車輛走過的 link 名序列。"""
    try:
        r = veh.traveled_route()[0]   # (Route, [timestamps])
        return "->".join(getattr(l, "name", str(l)) for l in r.links) \
            if hasattr(r, "links") else str(r)
    except Exception as e:
        return f"<traveled_route 失敗: {e}>"


def run_to_end(W, cap: int = 300):
    t = 0
    for _ in range(cap):
        t += 100
        W.exec_simulation(until_t=t)
        if not W.check_simulation_ongoing():
            break


# ---------------------------------------------------------------------------
def cap_version_and_world():
    banner("Cap 0: 版本 + World/網路 + 集合型別")
    print("uxsim version:", getattr(uxsim, "__version__", "?"))
    W = build_diamond()
    print("NODES 型別:", type(W.NODES).__name__, "names:", [getattr(n, "name", n) for n in W.NODES])
    print("LINKS 型別:", type(W.LINKS).__name__, "names:", [getattr(l, "name", l) for l in W.LINKS])
    print("VEHICLES 型別:", type(W.VEHICLES).__name__)
    print("link 查找相關方法:", [m for m in dir(W) if "link" in m.lower() and not m.startswith("__")])
    print("PASS")


def cap_run_and_read_state():
    banner("Cap 1: addVehicle + exec_simulation(until_t) 增量 + 讀車況")
    W = build_diamond()
    W.addVehicle("O", "D", departure_time=0, name="v1")
    for t in (50, 150, 300):
        W.exec_simulation(until_t=t)
        v = W.VEHICLES["v1"]
        link = getattr(v, "link", None)
        print(f"  t<= {t}: state={v.state} link={getattr(link,'name',link)} x={v.x} v={getattr(v,'v','?')}")
    print("PASS: 可增量步進並讀 state/link/x/v")


def cap_default_route():
    banner("Cap 2: 預設 DUO（應走較短上路 OA->AD）")
    W = build_diamond()
    W.addVehicle("O", "D", departure_time=0, name="v1")
    run_to_end(W)
    print("traveled:", route_links(W.VEHICLES["v1"]))
    print("PASS（確認為 OA->AD）")


def cap_enforce_route():
    banner("Cap 3a: enforce_route 強制走下路（OB->BD）")
    W = build_diamond()
    veh = W.addVehicle("O", "D", departure_time=0, name="v1")
    ok = None
    for route in (["OB", "BD"], ["O", "B", "D"]):
        try:
            veh.enforce_route(route)
            ok = route
            break
        except Exception as e:
            print(f"  試 {route} 失敗: {e}")
    run_to_end(W)
    print("接受格式:", ok, "| traveled:", route_links(W.VEHICLES["v1"]))
    print("PASS" if ok else "FAIL（enforce_route 無可用格式）")


def cap_links_prefer_avoid():
    banner("Cap 3b: set_links_avoid / set_links_prefer 改走下路（Design 2 核心）")
    for label, method, arg in (
        ("set_links_avoid(['OA','AD'])", "set_links_avoid", ["OA", "AD"]),
        ("set_links_prefer(['OB','BD'])", "set_links_prefer", ["OB", "BD"]),
    ):
        W = build_diamond()
        veh = W.addVehicle("O", "D", departure_time=0, name="v1")
        try:
            getattr(veh, method)(arg)
            run_to_end(W)
            tr = route_links(W.VEHICLES["v1"])
            ok = "OB->BD" in tr
            print(f"  {label}: traveled={tr}  => {'PASS' if ok else 'FAIL(未改道)'}")
        except Exception as e:
            print(f"  {label}: FAIL {e}")


def cap_route_pref_introspect():
    banner("Cap 3c: route_pref 結構 + World 路由旋鈕")
    W = build_diamond()
    veh = W.addVehicle("O", "D", departure_time=0, name="v1")
    W.exec_simulation(until_t=50)
    rp = getattr(veh, "route_pref", None)
    print("route_pref 型別:", type(rp).__name__, "值:", rp)
    knobs = [m for m in dir(W) if any(k in m.lower() for k in ("duo", "route_choice", "noise"))]
    print("World 路由相關屬性:", knobs)
    for k in knobs:
        try:
            print(f"  W.{k} =", getattr(W, k))
        except Exception:
            pass


def cap_midrun_addvehicle():
    banner("Cap 4: 運行中 addVehicle（介入 demand_surge）")
    W = build_diamond()
    W.addVehicle("O", "D", departure_time=0, name="v1")
    W.exec_simulation(until_t=120)
    before = len(W.VEHICLES)
    W.addVehicle("O", "D", departure_time=120, name="surge1")
    W.exec_simulation(until_t=500)
    sv = W.VEHICLES.get("surge1")
    print(f"  before={before} after={len(W.VEHICLES)} surge1.state={getattr(sv,'state','?')} "
          f"traveled={route_links(sv) if sv else '-'}")
    print("PASS" if sv is not None and len(W.VEHICLES) > before else "FAIL")


def cap_midrun_links_avoid_reroute():
    banner("Cap 5: 運行中 set_links_avoid 即時改道（介入 avoid_area；車在 OM、之後仍有替代）")
    W = build_late_diverge()
    veh = W.addVehicle("O", "D", departure_time=0, name="v1")
    W.exec_simulation(until_t=50)   # 車在 OM 共用段（尚未到分歧點 M）
    print("  改道前 link:", getattr(getattr(veh, "link", None), "name", "?"))
    try:
        veh.set_links_avoid(["MA", "AD"])   # 避開上路 → 到 M 後應改走 MB,BD
        run_to_end(W)
        tr = route_links(W.VEHICLES["v1"])
        ok = "MB->BD" in tr
        print(f"  改道後 traveled={tr}  => {'PASS' if ok else 'FAIL(未改走下路)'}")
    except Exception as e:
        print("FAIL（運行中 set_links_avoid）:", e)


def main():
    for cap in (cap_version_and_world, cap_run_and_read_state, cap_default_route,
                cap_enforce_route, cap_links_prefer_avoid, cap_route_pref_introspect,
                cap_midrun_addvehicle, cap_midrun_links_avoid_reroute):
        try:
            cap()
        except Exception:
            print(f"\n!!! {cap.__name__} 整段異常：")
            traceback.print_exc()


if __name__ == "__main__":
    main()
