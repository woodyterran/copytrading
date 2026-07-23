"""复现 2026-07-23 20:35 事故的回归测试：验证"成交佐证"能拒绝持续性脏读。

事故时序（BTC）：
  - 目标真实仓位 0.76557（20:33 买入后）
  - 节点持续返回过期快照 0.70557（低 0.06），期间目标无任何卖出成交
  - 旧逻辑据 0.70557 卖出 0.06；3 分钟后读数纠正到 0.76557 又买回 → 对敲
"""
import types
from hyperliquid_copy_trader import HyperliquidCopier


def make_copier():
    """绕过 __init__（含网络调用），只装配佐证所需的状态。"""
    c = object.__new__(HyperliquidCopier)
    c._target_fill_net = {}
    c._confirmed_target_pos = {}
    c._confirmed_fill_net = {}
    return c


def test_sustained_stale_read_is_rejected():
    c = make_copier()
    # 目标 20:33 买入 ~0.055，此处以真实持仓 0.76557 播种基准
    t_sz, seeded = c._corroborated_target_size("BTC", 0.76557)
    assert seeded is True and t_sz == 0.76557

    # 节点持续返回过期快照 0.70557，且期间无新成交 → 应判定脏读，仍返回 0.76557
    for _ in range(30):  # 模拟 3 分钟里多轮一致的脏读
        t_sz, seeded = c._corroborated_target_size("BTC", 0.70557)
        assert seeded is False
        assert abs(t_sz - 0.76557) < 1e-9, f"脏读未被拒绝，返回 {t_sz}"
    print("✅ 持续性脏读被正确拒绝，t_sz 始终锚定 0.76557（不会卖出）")


def test_real_fill_backed_change_is_followed():
    c = make_copier()
    c._corroborated_target_size("BTC", 0.76557)  # 播种

    # 目标真实买入 0.06（成交流 +0.06），随后快照升到 0.82557 → 应被佐证并采纳
    c._target_fill_net["BTC"] = 0.06
    t_sz, seeded = c._corroborated_target_size("BTC", 0.82557)
    assert seeded is False
    assert abs(t_sz - 0.82557) < 1e-9, f"真实变化未被采纳，返回 {t_sz}"
    print("✅ 有成交佐证的真实变化被正常跟随：0.76557 → 0.82557")


def test_partial_fills_accumulate():
    c = make_copier()
    c._corroborated_target_size("ETH", 10.0)  # 播种

    # 分批成交：先 +0.5 再 +0.5，快照逐步到 11.0 → 每步都被佐证
    c._target_fill_net["ETH"] = 0.5
    t_sz, _ = c._corroborated_target_size("ETH", 10.5)
    assert abs(t_sz - 10.5) < 1e-9
    c._target_fill_net["ETH"] = 1.0
    t_sz, _ = c._corroborated_target_size("ETH", 11.0)
    assert abs(t_sz - 11.0) < 1e-9
    print("✅ 分批成交逐步佐证正常")


if __name__ == "__main__":
    test_sustained_stale_read_is_rejected()
    test_real_fill_backed_change_is_followed()
    test_partial_fills_accumulate()
    print("\n全部通过。")
