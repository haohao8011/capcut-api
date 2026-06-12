"""切点策略：从 ASR/VAD 结果里挑出真正适合插素材的时机。

策略：
1. 以 VAD 停顿为首选切点
2. 如果停顿点过密，间隔小于 min_interval 的去掉
3. 如果整段没有停顿或数量不够，回退到按 fixed_interval 在主轨上均匀插
"""
from __future__ import annotations

from .models import CutPoint


def select_cut_points(
    cuts: list[CutPoint],
    *,
    min_interval: float = 2.5,
    max_cuts: int | None = None,
    total_duration: float | None = None,
    fallback_interval: float = 6.0,
) -> list[CutPoint]:
    """挑选最终使用的切点。

    - min_interval: 任意两个切点的最小间隔（秒）
    - max_cuts: 最多取多少个；超了就均匀采样
    - total_duration: 主轨总时长（用于回退均匀插点）
    - fallback_interval: 没有停顿切点时，按该间隔均匀分布
    """
    if not cuts and total_duration is not None and total_duration > 0:
        # 没有任何停顿，整段均匀切
        t = fallback_interval
        out: list[CutPoint] = []
        while t < total_duration:
            out.append(CutPoint(time=t, reason="fixed"))
            t += fallback_interval
        cuts = out

    # 间隔过滤：贪心，按时间排序后，相邻太近的丢掉
    cuts = sorted(cuts, key=lambda c: c.time)
    filtered: list[CutPoint] = []
    for c in cuts:
        if not filtered or (c.time - filtered[-1].time) >= min_interval:
            filtered.append(c)
        # 如果离上一个间隔不够，但理由是更长的 pause，可以替换上一个
        elif c.reason.startswith("pause") and c.time - filtered[-1].time >= 0.5:
            filtered[-1] = c
    cuts = filtered

    if max_cuts is not None:
        if max_cuts <= 0:
            return []
        if len(cuts) > max_cuts:
            # 均匀采样
            step = len(cuts) / max_cuts
            cuts = [cuts[int(i * step)] for i in range(max_cuts)]

    return cuts
