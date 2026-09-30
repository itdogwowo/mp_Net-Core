# 遙控配對發現邏輯 實施計劃

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓「遙控器（主控）↔ 執行端」的配對成為**雙邊一致**的狀態：解除綁定要通知對方、綁定後要能確認對方接納、並定義兩台主控搶同一台執行端時的行為。

**Architecture:** 三個既有機制已存在且可用 —— ① `0x100D` 廣播掃描（發現）、② `0x1016 SET_MASTER`（方向確認）、③ `node` 狀態的 btree 持久化（`role`/`master_cid`/`targets`）。本計劃**不新增任何指令**，只把這三者的語意補完整：`SET_MASTER{0xFFFF}` = 解除、綁定後用 `0x1101 STATUS_GET` 驗證、執行端對「已有 master」的處理規則。

**Tech Stack:** MicroPython（ESP32-S3）+ CPython 離線測試（`test/` 底下既有 stub 慣例）、NC4 協議（`Proto` / `SchemaCodec` / `bus`）、LVGL UI（`slave/ui/lvgl/page/remote.py`）。

---

## ⚠️ 三個可調的決策點（本計劃採用「推薦」欄，要改就改這裡）

計劃產出時你尚未選定這三項，我採用推薦值往下寫。每一項都標了「改這個會動到哪些 Task」。

| # | 決策 | 本計劃採用 | 替代方案 | 改動影響 |
|---|---|---|---|---|
| **D1** | 綁定後如何確認執行端接納 | **A：主控查 `0x1101 STATUS_GET{keys:"node.master_cid"}` 驗證**（零新增指令）| B：新增 `0x1019 PAIR_ACK`；C：執行端回 `0x1002` 公告 | 只有 Task 2。選 B 要多一個 Task（schema + handler + 測試）|
| **D2** | 兩台主控搶同一台執行端 | **需明確解除才能換 master** | 允許接手（舊主控事後發現）／後到直接搶（現狀）| 只有 Task 3 |
| **D3** | `0x1002 SLAVE_ANNOUNCE` 定位 | **不參與配對**，降級為「節點自我介紹」（顯示用 `pixel_count`/`hw_version`）| 也參與配對（主固件需補發送端）／直接廢掉 | Task 5 的文件描述；廢掉的話多加一個刪除 Task |

**為什麼 D1 選 A：** `0x1101` 的點分路徑查詢（`keys="node.master_cid"`）是 2026-09 剛做好的能力，而 `node` 已經在 `bus.shared` 裡（`ConfigManager.publish_node()`），**不需要任何新程式碼就能查**。新增 `0x1019` 只為了「一次確認」要付一條永久指令，違反「不重複設計」原則。

**為什麼 D2 選「需明確解除」：** 廣播掃描會讓射程內**所有**執行端都認掃描者為 master（`remote.py:129-132` 已註明這是刻意副作用）。若允許搶奪，第二台遙控器一掃描就會無聲搶走所有執行端，第一台完全不知道 —— 這在實務上等於「配對沒有意義」。拒絕 + 回報讓衝突變成**看得見**的事件。

---

## 現況（本計劃的起點，已驗證）

```
【已存在且可用】
  發現    主控 廣播 0x100D{reply_addr=自己cid}
            └→ 執行端 on_identify_req: 記 master_cid + 回 0x100E{cid,slave_id,ip}
            └→ 主控 PeerRegistry.learn_from_identify_rsp（含 ctx["_peer_mac"]）
  綁定    主控 0x1016{master_cid=自己cid} → 執行端 save_node() 落盤
  持久化  btree @node.role / @node.master_cid / @node.targets
  執行期  bus.role / bus.master_cid / bus.targets
  發布    bus.shared["node"]（UI 讀這一份）
  目標MAC 不另存，由 peers 表 by_cid() 查（ConfigManager.py:380 註明避免兩份真相）

【缺口】
  G1  _do_unbind() 只改本機 targets，沒有通知執行端 → 執行端 master_cid 永遠留著
  G2  綁定後主控不知道執行端有沒有接納（0x1016 是 fire-and-forget）
  G3  執行端對「已有 master 的新請求」無條件覆蓋 → 無聲搶奪
  G4  一對一/一對多的語意沒有寫死
  G5  0x1002 沒有發送端（主固件），定位不明
```

---

## File Structure

| 檔案 | 職責 | 本計劃的改動 |
|---|---|---|
| `slave/action/net_actions.py` | 網路/發現/方向相關指令 handler | `on_set_master` 加解除分支（Task 1）+ 已有 master 檢查（Task 3）|
| `slave/ui/lvgl/page/remote.py` | 遙控器頁（UI + 配對動作）| `_do_unbind` 通知對方（Task 1）、`_do_bind` 驗證（Task 2）、顯示配對狀態（Task 4）|
| `slave/lib/sys/ConfigManager.py` | node 狀態持久化 + 發布 | **改動極小**：`node_state()` 補一個 `paired` 欄位（Task 4）|
| `test/sys/test_remote_pairing.py` | **新增** —— 配對流程離線測試 | 全部 Task 的測試都放這裡 |
| `doc/01_protocol/02_command_index.md` | 指令索引 | `0x1016` 補 `0xFFFF` = 解除（Task 5）|
| `doc/03_notes/19_remote_control_plan.md` | 遙控計劃書 | §5/§11 更新配對流程（Task 5）|
| `todo/04_remote_pairing.md` | **新增** —— 測試追蹤清單（本計劃自身）| 建檔（Task 0）|

**測試檔位置的理由：** `test/sys/` 已有 `test_peer_registry.py`（同一個子系統的鄰居），且 `test/` 目錄**被 `.gitignore` 忽略** —— 與專案既有狀態一致（詳見「已知限制」）。

---

## Task 0: 建立追蹤清單

**Files:**
- Create: `todo/04_remote_pairing.md`

- [ ] **Step 1: 建立清單檔**

複製 `todo/_template.md` 的結構，寫入：

```markdown
# 04 — 遙控配對發現邏輯

> **範圍**：遙控器（主控）↔ 執行端 的配對／解除／衝突處理。實作計劃見本檔各 Task。
> **設計唯一真相**：`doc/03_notes/19_remote_control_plan.md`
> **最後更新**：2026-09

## 設計決策（採用值）

| # | 決策 | 採用 |
|---|---|---|
| D1 | 綁定後如何確認接納 | 主控查 `0x1101 STATUS_GET{keys:"node.master_cid"}` |
| D2 | 搶奪行為 | 需明確解除才能換 master |
| D3 | `0x1002` 定位 | 不參與配對（降級為顯示用自我介紹）|

## 待測項目

- [ ] T1 `0x1016{master_cid:0xFFFF}` = 解除，執行端清 master_cid 並落盤
- [ ] T2 綁定後狀態一致（主控查詢 == 自己的 cid）
- [ ] T3 執行端已有 master 時拒絕新請求，且主控看得見拒絕
- [ ] T4 UI 顯示配對狀態（未配對／已配對／被佔用）
- [ ] T5 真機：兩台遙控器 + 一台執行端的衝突實測
- [ ] T6 真機：解除後執行端不再回覆舊主控

## 離線自測

```bash
python -B -m unittest discover -s test/sys -p "test_remote_pairing*.py"
```

## 真機驗證（待補）

- [ ] 單主控單執行端：掃描 → 綁定 → 確認 → 解除 → 確認
- [ ] 雙主控搶奪：A 綁定 → B 綁定（應被拒）→ A 解除 → B 綁定（應成功）
```

- [ ] **Step 2: 更新 `todo/README.md` 的索引**

在「清單索引」表格加一列：

```markdown
| [04_remote_pairing.md](04_remote_pairing.md) | 遙控配對發現邏輯（0x100D/0x1016/0x1101） | 設計完成，實作待做 |
```

- [ ] **Step 3: Commit**

```bash
git add todo/04_remote_pairing.md todo/README.md
git commit -m "docs: 遙控配對發現邏輯追蹤清單（todo/04）"
```

---

## Task 1: 解除綁定要雙邊同步（零新增指令）

**Files:**
- Modify: `slave/action/net_actions.py:219-241`（`on_set_master`）
- Modify: `slave/ui/lvgl/page/remote.py:186-213`（`_do_unbind`）
- Test: `test/sys/test_remote_pairing.py`（新增）

**背景：** `0xFFFF` 在本專案**本來就是「未設定」的哨兵** —— `bus.master_cid` 初值（`sys_bus.py:23`）、`node_state()` 的 `bound = mcid != 0xFFFF`（`ConfigManager.py:406`）、`_reply()` 的預設回應位址都是它。所以「`SET_MASTER{0xFFFF}` = 解除」不是新規則，是**讓既有語意完整**。

- [ ] **Step 1: 寫失敗的測試**

建立 `test/sys/test_remote_pairing.py`：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""遙控配對發現邏輯的 PC 自檢（不依賴 MicroPython / 硬體）

驗證雙邊一致的配對狀態：
  T1 on_set_master{0xFFFF} = 解除（清 master_cid、清 role、落盤）
  T2 on_set_master{有效cid} = 綁定（記 master_cid、role="slave"）
  T3 已有 master 時拒絕新請求（D2）
  T4 _do_unbind 會發射解除指令給對方

用法: python -B -m unittest discover -s test/sys -p "test_remote_pairing*.py"
"""

import os
import sys
import types
import unittest

# ── MicroPython stub（沿用 test/sys/test_peer_registry.py 的慣例）──
sys.modules.setdefault('micropython', types.SimpleNamespace(
    native=lambda f: f, viper=lambda f: f, const=lambda v: v))
import builtins                                                    # noqa: E402
if not hasattr(builtins, "ptr8"):
    builtins.ptr8 = lambda b: b
    builtins.ptr16 = lambda b: b

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, os.path.join(_ROOT, "slave"))

from lib.sys.sys_bus import bus                                    # noqa: E402

ADDR_BROADCAST = 0xFFFF
MASTER_CID = 0x1234
SLAVE_CID = 0x5678


class TestSetMasterUnbind(unittest.TestCase):
    """T1/T2: 0x1016 的綁定與解除語意。"""

    def setUp(self):
        bus.master_cid = ADDR_BROADCAST
        bus.role = None

    def test_bind_sets_master_and_role(self):
        """有效 cid → 記 master_cid、本板是被控方。"""
        from action import net_actions
        net_actions.on_set_master({"app": None, "send": lambda f: None},
                                  {"master_cid": MASTER_CID})
        self.assertEqual(bus.master_cid, MASTER_CID)
        self.assertEqual(bus.role, "slave")

    def test_unbind_clears_master_and_role(self):
        """★ 核心改動：0xFFFF = 解除（清乾淨，不是設成廣播位址）。"""
        from action import net_actions
        # 先綁定
        net_actions.on_set_master({"app": None, "send": lambda f: None},
                                  {"master_cid": MASTER_CID})
        # 再解除
        net_actions.on_set_master({"app": None, "send": lambda f: None},
                                  {"master_cid": ADDR_BROADCAST})
        self.assertEqual(bus.master_cid, ADDR_BROADCAST,
                         "解除後 master_cid 回到未設定的哨兵")
        self.assertIsNone(bus.role, "解除後 role 應清空（不再是 slave）")

    def test_unbind_is_idempotent(self):
        """沒綁定時解除不該炸。"""
        from action import net_actions
        net_actions.on_set_master({"app": None, "send": lambda f: None},
                                  {"master_cid": ADDR_BROADCAST})
        self.assertEqual(bus.master_cid, ADDR_BROADCAST)


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: 執行測試，確認失敗**

```bash
python -B -m unittest discover -s test/sys -p "test_remote_pairing*.py" -v
```

預期：`test_unbind_clears_master_and_role` **FAIL**

```
AssertionError: 'slave' is not None : 解除後 role 應清空（不再是 slave）
```

（現行程式碼 `if mc != ADDR_BROADCAST and not getattr(bus, "role", None): bus.role = "slave"` —— 解除時 `mc == 0xFFFF` 所以 role 沒被改，停在 `"slave"`。另兩個測試應該 PASS。）

- [ ] **Step 3: 寫最小實作**

改 `slave/action/net_actions.py` 的 `on_set_master`：

```python
def on_set_master(ctx, args):
    """0x1016: 顯式設定回應定址 master_cid（＝**方向確認**）。

    payload 是**對方的 cid**，語意是「你的 master 是我」。
    所以本板收到它 → 記住對方位址，且**本板是被控方**（role = "slave"）。
    反之，本板主動發它 → 是告訴對方「你的 master 是我」。

    ★ `master_cid == 0xFFFF` = **解除配對**（不是「設成廣播」）。
      `0xFFFF` 在本專案本來就是「未設定」的哨兵（`bus.master_cid` 初值、
      `node_state()` 的 `bound` 推導），所以這不是新規則 —— 是讓既有語意完整。
      解除要**清乾淨**：master_cid 回哨兵、role 清空，否則本板會停在
      「我是某人的 slave」但其實沒有 master 的矛盾狀態。

    ★ 這裡是**唯一會持久化方向的地方**：
      - 本指令是明確動作（人按了綁定/解除，或對方明確告知）→ 值得寫 flash
      - `0x100D IDENTIFY_REQ` 的 `reply_addr` 是隱含版本（每次敲門都來）
        → 只寫記憶體，不落盤（見 on_identify_req）
    持久化失敗不影響本次設定（記憶體已生效），只印訊息。
    """
    mc = args.get("master_cid", 0xFFFF) & 0xFFFF
    bus.master_cid = mc
    if mc == ADDR_BROADCAST:
        # 解除：清掉「被控方」身分（見 docstring）
        bus.role = None
    elif not getattr(bus, "role", None):
        # 有人明確告知方向 → 本板是被控方；MAC 之後由 peers 表 by_cid() 查
        bus.role = "slave"
    try:
        from lib.sys.ConfigManager import cfg_manager
        cfg_manager.save_node()
    except Exception as e:
        print("[Net] SET_MASTER 持久化失敗（記憶體仍生效）: {}".format(e))
```

- [ ] **Step 4: 執行測試，確認通過**

```bash
python -B -m unittest discover -s test/sys -p "test_remote_pairing*.py" -v
```

預期：`Ran 3 tests ... OK`

- [ ] **Step 5: 讓 `_do_unbind` 通知對方**

改 `slave/ui/lvgl/page/remote.py` 的 `_do_unbind` —— 在改本機 targets **之前**先發射解除：

```python
def _do_unbind():
    """從 targets 移除選中的節點，並**通知對方解除**（雙邊一致）。

    ★ 為什麼一定要通知：配對是**雙邊狀態**（本機 targets + 對方的 master_cid）。
      只改本機的話，對方會永遠把回應送給一台已經不要它的主控
      —— 這是本專案先前漏掉的半邊（`todo/04` G1）。

    解除用既有的 `0x1016`，`master_cid = 0xFFFF`（未設定的哨兵）＝解除。
    """
    b = _kv()
    p = _selected()
    old_targets = list(getattr(b, "targets", None) or [])
    cur = list(old_targets)
    if not cur:
        print("[remote] 目前沒有目標")
        return
    if p is not None and p.get("cid") is not None:
        cid = int(p["cid"]) & 0xFFFF
        new = [t for t in cur if t.get("cid") != cid]
        if len(new) == len(cur):
            # 選中的節點不是目標 → 退而解除「目前 active」那一個（比較符合直覺）
            new = [t for t in cur if not t.get("active")]
            print("[remote] 選中的不是目標 → 解除目前 active")
        cur = new
    else:
        cur = []                       # 沒選中 → 全部清掉

    # ① 通知每一個被解除的對方（射頻層；用 peer 的 mac 定向）
    #    ⚠️ 用 cid 集合比對，不要用 `t not in cur` —— dict 的 `in` 是逐值相等，
    #    若兩個 target 內容剛好相同會漏判。cid 才是身分。
    kept = {int(t.get("cid") or 0) & 0xFFFF for t in cur}
    removed = [t for t in old_targets
               if (int(t.get("cid") or 0) & 0xFFFF) not in kept]
    for t in removed:
        peer = {"cid": t.get("cid"), "mac": t.get("mac")}
        ok = _tx(0x1016, {"master_cid": 0xFFFF}, peer)
        print("[remote] 解除通知 0x{:04X} → {}".format(
            int(t.get("cid") or 0) & 0xFFFF, ok))

    # ② 本地：移除目標
    if cur and not any(t.get("active") for t in cur):
        cur[0]["active"] = True        # 至少留一個 active
    b.targets = cur
    b.role = "master" if cur else None
    try:
        from lib.sys.ConfigManager import cfg_manager
        cfg_manager.save_node()
    except Exception:
        pass
    print("[remote] 解除綁定 → 剩 {} 個目標".format(len(cur)))
```

- [ ] **Step 6: 語法檢查 + 全套測試**

```bash
python -B -c "import ast; [ast.parse(open(f,encoding='utf-8').read(), filename=f) for f in ['slave/action/net_actions.py','slave/ui/lvgl/page/remote.py']]; print('SYNTAX OK')"
python -B -m unittest discover -s test/sys -p "test_*.py"
```

預期：`SYNTAX OK`，且 `test_peer_registry` 與 `test_remote_pairing` 都 OK。

⚠️ **不要用 `python -m py_compile`** —— 它會強制寫 `.pyc`，`-B` 擋不住（專案禁止 `__pycache__`）。用 `ast.parse`。

- [ ] **Step 7: Commit**

```bash
git add slave/action/net_actions.py slave/ui/lvgl/page/remote.py test/sys/test_remote_pairing.py
git commit -m "feat(pairing): 0x1016{0xFFFF} = 解除配對，解除時通知對方（雙邊一致）"
```

---

## Task 2: 綁定後驗證對方接納（D1 = 方案 A，零新增指令）

**Files:**
- Modify: `slave/ui/lvgl/page/remote.py`（`_do_bind` + 新增 `_verify_pairing`）
- Test: `test/sys/test_remote_pairing.py`（追加）

**背景：** `0x1016` 是 fire-and-forget —— 主控送出去就當成功。但執行端可能沒收到、或（Task 3 之後）拒絕。用 `0x1101 STATUS_GET{keys:"node.master_cid"}` 查對方的 `node.master_cid`，比對是否等於自己的 cid。

`node` 已在 `bus.shared`（`ConfigManager.publish_node()`），而 `0x1101` 的點分路徑查詢是既有能力 —— **不需要任何新的 handler 或 schema**。

- [ ] **Step 1: 寫失敗的測試**

在 `test/sys/test_remote_pairing.py` 追加：

```python
class TestPairingVerify(unittest.TestCase):
    """T2: 綁定後的驗證邏輯（純函式，不碰 UI/射頻）。"""

    def test_verify_matches_own_cid(self):
        from ui.lvgl.page.remote import _pairing_verdict
        self.assertTrue(_pairing_verdict(MASTER_CID, MASTER_CID))

    def test_verify_rejects_other_master(self):
        """對方回報的 master 不是自己 → 沒配對成功。"""
        from ui.lvgl.page.remote import _pairing_verdict
        self.assertFalse(_pairing_verdict(MASTER_CID, 0x9999))

    def test_verify_rejects_unpaired(self):
        """對方回報 0xFFFF（未配對）→ 沒成功。"""
        from ui.lvgl.page.remote import _pairing_verdict
        self.assertFalse(_pairing_verdict(MASTER_CID, ADDR_BROADCAST))
        self.assertFalse(_pairing_verdict(MASTER_CID, None))
```

- [ ] **Step 2: 執行測試，確認失敗**

```bash
python -B -m unittest discover -s test/sys -p "test_remote_pairing*.py" -v
```

預期：`ImportError: cannot import name '_pairing_verdict'`

⚠️ **這個測試會 import `remote.py`**，而它依賴 LVGL。若 import 失敗（`ModuleNotFoundError: lvgl`），先確認 `remote.py` 的 module-level import 有哪些 —— 若無法在 CPython 下 import，**改用 AST 靜態檢查**（比照 `test/protocol/test_now_1301_action.py` 的 `TestLocalCallerTrap` 手法），而不是為測試去改產品程式碼。判斷方式：

```bash
python -B -c "import sys; sys.path.insert(0,'slave'); import ui.lvgl.page.remote"
```

- [ ] **Step 3: 寫實作**

在 `slave/ui/lvgl/page/remote.py` 加（放在 `_do_bind` 之前）：

```python
def _pairing_verdict(my_cid, peer_master_cid):
    """判定「對方是否已接納我為 master」。純函式，好測。

    peer_master_cid 來自對方的 `0x1101 STATUS_GET{keys:"node.master_cid"}`。
      None      → 查不到（對方沒回應／不支援）
      0xFFFF    → 對方未配對（哨兵值）
      == my_cid → ✅ 配對成功
      其他      → ❌ 對方認的是別人（被別台主控佔用）
    """
    if peer_master_cid is None:
        return False
    try:
        return (int(peer_master_cid) & 0xFFFF) == (int(my_cid) & 0xFFFF)
    except Exception:
        return False
```

然後在 `_do_bind()` 的結尾（`print("[remote] 綁定 ...")` 之後）加：

```python
    # ④ 驗證：查對方的 node.master_cid，確認它真的接納了（D1 方案 A）
    #    0x1016 是 fire-and-forget，所以這一步是「看得見的成功」。
    _verify_pairing(tgt["cid"], my_cid)


def _verify_pairing(peer_cid, my_cid):
    """向對方查 `node.master_cid`，比對是否為自己。

    ★ 零新增指令：`node` 已在 bus.shared，`0x1101` 的點分路徑查詢是既有能力。
    ★ 查不到（逾時／對方不支援）**不算失敗** —— 只回報「無法確認」，
      因為舊版執行端沒有這個 provider。這是刻意的寬容。
    """
    got = _query_peer_key(peer_cid, "node.master_cid")
    if _pairing_verdict(my_cid, got):
        print("[remote] ✅ 配對確認：對方 master=0x{:04X}".format(my_cid))
    elif got is None:
        print("[remote] ⚠️ 無法確認配對（對方沒回應 node.master_cid）")
    else:
        print("[remote] ❌ 配對未生效：對方回報 master=0x{:04X}".format(
            int(got) & 0xFFFF))
    return got
```

⚠️ **`_query_peer_key` 需要你依實際的回應接收機制實作** —— 本專案的 `0x1102 STATUS_RSP` 是非同步進來的（寫進某個共享狀態），沒有一個「問了就等答案」的同步 API。**先確認回應怎麼進來的**：

```bash
grep -rn "0x1102\|STATUS_RSP" slave/tasks/ slave/action/status_actions.py
```

若沒有現成的請求-回應配對機制，**Task 2 降級為**：只送查詢並把結果留給下一次 UI 刷新時比對（即 `_verify_pairing` 拆成「發查詢」與「收到後判定」兩半，判定用 `_pairing_verdict`）。此時 Step 1 的測試（純函式）仍然有效且足夠 —— **不要為了同步等待而去加阻塞輪詢**（會卡住 UI task）。

- [ ] **Step 4: 執行測試，確認通過**

```bash
python -B -m unittest discover -s test/sys -p "test_remote_pairing*.py" -v
```

預期：`Ran 6 tests ... OK`

- [ ] **Step 5: Commit**

```bash
git add slave/ui/lvgl/page/remote.py test/sys/test_remote_pairing.py
git commit -m "feat(pairing): 綁定後查 node.master_cid 驗證對方接納（D1 方案 A）"
```

---

## Task 3: 執行端拒絕搶奪（D2 = 需明確解除）

**Files:**
- Modify: `slave/action/net_actions.py`（`on_set_master` 加已有 master 檢查）
- Test: `test/sys/test_remote_pairing.py`（追加）

**背景：** 目前執行端對任何 `0x1016` 都無條件接受。有了 Task 2 的驗證，主控就**看得見**「對方認的是別人」。

- [ ] **Step 1: 寫失敗的測試**

```python
class TestConflictPolicy(unittest.TestCase):
    """T3: 執行端已有 master 時的行為（D2 = 需明確解除）。"""

    def setUp(self):
        bus.master_cid = ADDR_BROADCAST
        bus.role = None

    def test_reject_new_master_when_already_paired(self):
        """已有 master 時，新的請求被拒絕（要換必須先解除）。"""
        from action import net_actions
        net_actions.on_set_master({"app": None, "send": lambda f: None},
                                  {"master_cid": MASTER_CID})
        other = 0x9999
        net_actions.on_set_master({"app": None, "send": lambda f: None},
                                  {"master_cid": other})
        self.assertEqual(bus.master_cid, MASTER_CID,
                         "★ 已有 master 時不該被搶走")

    def test_same_master_rebind_is_ok(self):
        """同一個 master 重複綁定 → 當成確認，不算衝突（idempotent）。"""
        from action import net_actions
        net_actions.on_set_master({"app": None, "send": lambda f: None},
                                  {"master_cid": MASTER_CID})
        net_actions.on_set_master({"app": None, "send": lambda f: None},
                                  {"master_cid": MASTER_CID})
        self.assertEqual(bus.master_cid, MASTER_CID)

    def test_after_unbind_new_master_accepted(self):
        """解除後就能被新的 master 綁定。"""
        from action import net_actions
        net_actions.on_set_master({"app": None, "send": lambda f: None},
                                  {"master_cid": MASTER_CID})
        net_actions.on_set_master({"app": None, "send": lambda f: None},
                                  {"master_cid": ADDR_BROADCAST})
        other = 0x9999
        net_actions.on_set_master({"app": None, "send": lambda f: None},
                                  {"master_cid": other})
        self.assertEqual(bus.master_cid, other)
```

- [ ] **Step 2: 執行測試，確認失敗**

```bash
python -B -m unittest discover -s test/sys -p "test_remote_pairing*.py" -v
```

預期：`test_reject_new_master_when_already_paired` **FAIL**（現況會被搶走）

- [ ] **Step 3: 寫實作**

改 `slave/action/net_actions.py` 的 `on_set_master`，加入拒絕邏輯（放在 `mc = ...` 之後）：

```python
    mc = args.get("master_cid", 0xFFFF) & 0xFFFF
    cur = int(getattr(bus, "master_cid", ADDR_BROADCAST)) & 0xFFFF

    # ★ 已有 master 時拒絕**新的** master（D2：要換必須先明確解除）。
    #   理由：廣播掃描會讓射程內所有執行端都認掃描者為 master
    #   （remote.py `_do_scan` 註明的刻意副作用）。若允許搶奪，第二台遙控器
    #   一掃描就無聲搶走所有執行端，第一台完全不知道 —— 配對等於沒意義。
    #   拒絕讓衝突變成**看得見**的事件（主控可用 0x1101 查到對方認的是誰）。
    #   同一個 master 重複綁定 = 確認，不算衝突（idempotent）。
    if mc != ADDR_BROADCAST and cur != ADDR_BROADCAST and cur != mc:
        print("[Net] SET_MASTER 拒絕：已被 0x{:04X} 佔用（要換先解除）".format(cur))
        return

    bus.master_cid = mc
    if mc == ADDR_BROADCAST:
        bus.role = None
    elif not getattr(bus, "role", None):
        bus.role = "slave"
    try:
        from lib.sys.ConfigManager import cfg_manager
        cfg_manager.save_node()
    except Exception as e:
        print("[Net] SET_MASTER 持久化失敗（記憶體仍生效）: {}".format(e))
```

- [ ] **Step 4: 執行測試，確認通過**

```bash
python -B -m unittest discover -s test/sys -p "test_remote_pairing*.py" -v
```

預期：`Ran 9 tests ... OK`

- [ ] **Step 5: ⚠️ 拒絕無法用 `_reply()` 回報 —— 主控要靠查詢自己發現**

先確認這個既有限制（**不要試圖用 `_reply` 送拒絕**）：

```python
# net_actions.py:43-53（既有）
def _reply(ctx, rsp_cmd, fields):
    """送出回應幀, addr 回 bus.master_cid (未設=0xFFFF 廣播)。"""
    ctx["send"](Proto.pack(rsp_cmd, payload, addr=bus.master_cid))
```

被拒絕時 `bus.master_cid` **還是舊 master**，所以任何 `_reply` 都會送到舊 master 那裡，**新的請求者永遠收不到拒絕**。而 `0x1016` 目前也沒有對應的 RSP 指令。

**所以「拒絕」的可見性是這樣成立的**（也是 D1 選 A 的另一個好處）：

```
新主控B  發 0x1016{master_cid=B}
執行端   已有 A  → 拒絕（只印 log，狀態不變）
新主控B  查 0x1101{keys:"node.master_cid"}
執行端   回 {node.master_cid: A}
新主控B  _pairing_verdict(B, A) == False  → 「❌ 配對未生效：對方回報 master=0xAAAA」
```

→ **Task 2 的驗證機制就是衝突的可見性機制**，不需要新指令。在 `on_set_master` 的 docstring 註明這件事：

```python
    ★ 「拒絕」為什麼不回報：被拒絕時 `bus.master_cid` 還是**舊** master，
      而 `_reply()` 一律送到 `bus.master_cid` —— 所以回覆會跑到舊 master 那裡，
      新的請求者永遠收不到。且 0x1016 沒有對應的 RSP 指令。
      請求者要用 `0x1101 STATUS_GET{keys:"node.master_cid"}` 查對方認的是誰
      （見 remote._verify_pairing）—— 拒絕因此是**看得見**的。
```

- [ ] **Step 6: 更新 `0x1016` 的 docstring**

把「已有 master 時拒絕」與「0xFFFF = 解除」兩件事補進 `on_set_master` 的 docstring（見 Task 1 Step 3 的版本，在其後追加 D2 與 Step 5 的「拒絕不回報」段落）。

- [ ] **Step 7: Commit**

```bash
git add slave/action/net_actions.py test/sys/test_remote_pairing.py
git commit -m "feat(pairing): 執行端已有 master 時拒絕新請求（D2 需明確解除）"
```

---

## Task 4: UI 顯示配對狀態

**Files:**
- Modify: `slave/lib/sys/ConfigManager.py:385-408`（`node_state`）
- Modify: `slave/ui/lvgl/page/remote.py`（節點清單顯示）
- Test: `test/sys/test_remote_pairing.py`（追加）

**背景：** 使用者需要看見「這台節點是：未配對 / 已配對我 / 被別人佔用」。`node_state()` 目前只有 `bound`（自己有沒有 master），缺「配對給誰」。

- [ ] **Step 1: 寫失敗的測試**

```python
class TestNodeStatePairing(unittest.TestCase):
    """T4: node_state 要能表達「配對給誰」。"""

    def test_node_state_has_paired_to(self):
        from lib.sys.ConfigManager import cfg_manager
        bus.master_cid = MASTER_CID
        st = cfg_manager.node_state()
        self.assertIn("paired_to", st)
        self.assertEqual(st["paired_to"], MASTER_CID)

    def test_node_state_paired_to_none_when_unbound(self):
        from lib.sys.ConfigManager import cfg_manager
        bus.master_cid = ADDR_BROADCAST
        st = cfg_manager.node_state()
        self.assertIsNone(st["paired_to"])
```

- [ ] **Step 2: 執行測試，確認失敗**

```bash
python -B -m unittest discover -s test/sys -p "test_remote_pairing*.py" -v
```

預期：`KeyError: 'paired_to'` 或 `AssertionError`

- [ ] **Step 3: 寫實作**

改 `ConfigManager.node_state()` 的 `return` dict，加一個欄位：

```python
        return {
            "cid": cid,
            "mac": mac,
            "hostname": sys_cfg.get("hostname", ""),
            "role": getattr(self.bus, "role", None),
            "master_cid": mcid,
            "bound": mcid != 0xFFFF,
            # paired_to：配對給誰（None = 未配對）。與 bound 語意重疊但更好讀，
            #   且讓 UI 不必自己讀 0xFFFF 的哨兵語意（D1 的驗證也用它）。
            "paired_to": None if mcid == 0xFFFF else mcid,
            "targets": list(getattr(self.bus, "targets", None) or []),
        }
```

- [ ] **Step 4: 執行測試，確認通過**

```bash
python -B -m unittest discover -s test/sys -p "test_remote_pairing*.py" -v
```

預期：`Ran 11 tests ... OK`

- [ ] **Step 5: UI 顯示**

在 `remote.py` 的節點清單繪製處（`_on_list_move` / 清單更新附近）顯示狀態。**先找出目前節點列是怎麼畫的**：

```bash
grep -n "_peer_rows\|_peer_btns\|mk_list\|def _refresh" slave/ui/lvgl/page/remote.py
```

然後在節點列的標籤後面加狀態後綴（依 `node.master_cid` 與本機 `cid` 比對）：

```python
def _pair_label(peer):
    """節點列的配對狀態後綴（給 UI 用）。

    peer["paired_to"] 由對方的 node 狀態來（0x1101 查得）；
    沒有這個資訊時退回「?」，不要猜。
    """
    pt = peer.get("paired_to")
    if pt is None:
        return "?"                      # 未知（還沒查過）
    if int(pt) & 0xFFFF == ADDR_BROADCAST:
        return "未配對"
    b = _kv()
    if (int(pt) & 0xFFFF) == (int(getattr(b, "cid", 0xFFFF)) & 0xFFFF):
        return "已配對"
    return "佔用 0x{:04X}".format(int(pt) & 0xFFFF)
```

- [ ] **Step 6: 離線 UI smoke（照既有慣例）**

專案既有做法是用 stub 驅動 UI（`Test_Peer/README.md` 提到 `ui_smoke.py`）。若 `/tmp/nodetest/ui_smoke.py` 不存在於本機，**跳過此步並在 `todo/04_remote_pairing.md` 記為待補**，不要為了它新建一整套 UI stub 框架。

- [ ] **Step 7: Commit**

```bash
git add slave/lib/sys/ConfigManager.py slave/ui/lvgl/page/remote.py test/sys/test_remote_pairing.py
git commit -m "feat(pairing): node_state 補 paired_to，UI 顯示配對狀態"
```

---

## Task 5: 文件同步

**Files:**
- Modify: `doc/01_protocol/02_command_index.md`（`0x1016` 那列）
- Modify: `doc/03_notes/19_remote_control_plan.md`（§5 / §11）

- [ ] **Step 1: 更新指令索引**

`doc/01_protocol/02_command_index.md` 找到 `0x1016 SET_MASTER` 那列，補上：

```markdown
| 0x1016 | SET_MASTER | Server → MCU | `master_cid(u16)` | 方向確認：告知對方「你的 master 是我」。**`0xFFFF` = 解除配對**（清 master_cid + role）。已有 master 時拒絕新的（要換先解除）|
```

- [ ] **Step 2: 更新計劃書**

`doc/03_notes/19_remote_control_plan.md` 的 §5（缺口清單）追加一列，並更新 §11（待決問題）—— 把「`0x1002` 要不要帶 cid」的條目標記為「**已決**：不再需要，配對走 `0x100D`（cid 直接有），`0x1002` 降級為顯示用自我介紹（D3）」。

- [ ] **Step 3: 驗證沒有殘留的舊描述**

```bash
grep -rn "SET_MASTER" doc/ | grep -v "0xFFFF"
```

預期：沒有「只寫記憶體不落盤」之類與新行為矛盾的描述。

- [ ] **Step 4: Commit**

```bash
git add doc/01_protocol/02_command_index.md doc/03_notes/19_remote_control_plan.md
git commit -m "docs: 配對語意（0x1016 解除 + 拒絕搶奪）同步到指令索引與計劃書"
```

---

## Task 6: 全量回歸 + 追蹤清單收尾

**Files:**
- Modify: `todo/04_remote_pairing.md`

- [ ] **Step 1: 跑所有離線測試**

```bash
python -B -m unittest discover -s test/sys -p "test_*.py" -v
python -B -m unittest discover -s test/protocol -p "test_now_1301*.py" -v
```

預期：全部 OK（`test_peer_registry` + `test_remote_pairing` + `test_now_1301_action`）

- [ ] **Step 2: 確認沒有污染**

```bash
git status --porcelain
find . -name "__pycache__" -not -path "./tools/WebMaster/.venv/*" 2>/dev/null
```

預期：沒有 `__pycache__`（專案禁止）；`git status` 只有本計劃預期的檔案。

- [ ] **Step 3: 更新追蹤清單**

把 `todo/04_remote_pairing.md` 的 T1~T4 勾 `[x]`（**離線自測通過**），T5/T6 保持 `[ ]`（真機待做），並註明：

```markdown
> ⚠️ 離線自測通過 ≠ 實測完成。依 `todo/README.md` 的慣例，真機驗證前不勾實測項。
```

- [ ] **Step 4: Commit**

```bash
git add todo/04_remote_pairing.md
git commit -m "test(pairing): 離線自測完成，真機驗證待做（todo/04）"
```

---

## 已知限制（不在本計劃範圍）

| # | 限制 | 為什麼不在範圍 |
|---|---|---|
| L1 | `_query_peer_key` 的請求-回應配對 | 專案目前沒有同步查詢 API。Task 2 Step 3 給了降級方案（送查詢 + 非同步判定）。要做成同步需要一個 request/response 配對層，那是獨立子系統 |
| L2 | 執行端主動發現主控 | **未選定**（D-發現方向）。本計劃只做主控主動 |
| L3 | 多 master（執行端記多個）| **未選定**（D-關係）。本計劃維持單一 `master_cid` |
| L4 | 配對的加密/認證 | 目前任何人掃描就能配對。ESP-NOW 本身已有 `add_peer` 白名單，但沒有配對密碼／確認碼機制 |
| L5 | `0x1303 NOW_STATS` → `now_stats` provider | 已定案但**延後**（優先做配對，見對話記錄）|
| L6 | `test/` 被 `.gitignore` 忽略 | 新測試檔在本機有效但**不會進版控**（`test/sys/test_peer_registry.py` 也一樣）。要納入得 `git add -f`，或改 `.gitignore` 的第 227 行 `test` 規則 —— **這是獨立決定，本計劃不動** |

---

## Self-Review（計劃寫完後的自我檢查）

**1. Spec coverage** — 對照四個設計缺口：

| 缺口 | 覆蓋它的 Task |
|---|---|
| G1 `_do_unbind` 只解除單邊 | Task 1 ✅ |
| G2 綁定後無法確認接納 | Task 2 ✅ |
| G3 無條件覆蓋（無聲搶奪）| Task 3 ✅ |
| G4 一對一/一對多語意未定 | Task 5（文件寫死現行語意）+ 本檔「已知限制 L3」|
| G5 `0x1002` 定位不明 | Task 5 Step 2（標記為已決：D3 = 不參與配對）|

**2. Placeholder scan** — 已檢查：
- ⚠️ **Task 2 Step 3 的 `_query_peer_key` 沒有完整實作** —— 這是**刻意的**，因為它取決於「回應怎麼進來」的既有機制，計劃中給了明確的調查指令（`grep`）與降級方案。**不是**「TBD」式的偷懶，而是把未知誠實標記出來。
- ⚠️ **Task 4 Step 5 的 UI 插入點**沒有給精確行號 —— 給了 `grep` 指令定位。`remote.py` 是 568 行的 UI 檔，精確行號會隨其他改動漂移。
- 其餘每個改動步驟都有完整程式碼。

**3. Type consistency** — 已檢查：
- `ADDR_BROADCAST`（`net_actions.py` 既有常數，Task 1/3 使用）✅
- `_pairing_verdict(my_cid, peer_master_cid)` —— Task 2 定義、Task 2 測試用 ✅
- `node_state()["paired_to"]` —— Task 4 定義、Task 4 測試與 UI 用 ✅
- `peer["paired_to"]`（Task 4 Step 5 的 `_pair_label`）與 `node_state()["paired_to"]`（Task 4 Step 3）**同名同義** ✅
- `bus.master_cid` / `bus.role` / `bus.targets` —— 全部是既有屬性，無新增 ✅

---

## 執行方式

計劃已存到 `todo/04_remote_pairing.md`。兩種執行選項：

1. **Subagent-Driven（推薦）** —— 每個 Task 派一個 fresh subagent，Task 之間我審查，迭代快
2. **Inline Execution** —— 在這個 session 逐 Task 執行，批次到檢查點讓你審查

**要哪一種？** 或者你想先改上面那三個決策點（D1/D2/D3）再開工？
