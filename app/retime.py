"""按 source/target 时间锚点对 cue 时间轴做分段线性校时。

锚点 ``(source, target)`` 两两配对，两列时间均非负且严格递增
（由请求模型保证；本模块只负责映射本身）：

* 时间落在某个锚点区间 ``[source_k, source_{k+1}]`` 内时，按该区间
  线性插值到 target 列；
* 恰好等于某个 source 锚点时，直接映射到对应 target；
* 早于首个锚点或晚于末个锚点时，沿**最近**的首/末区间斜率外推；
* 每条 cue 的 start、end 相互独立、各自映射——即使 cue 跨越一个或
  多个锚点，两端也分别落在各自所在的区间上计算。

映射在数学上严格单调递增，因此正常浮点结果一定保持区间合法、
非负；但外推可能得到负时间（源时间早于首锚点且首区间向上），
极端的退化斜率配合浮点舍入也可能使 end 落到 start 之前。
出现以上任一情况时收集全部相关 cue id，由调用方返回 409，
不输出部分结果。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field

from .models import Cue, TimeAnchor


@dataclass(frozen=True)
class _Segment:
    """一个锚点区间及其线性映射参数：target = slope * source + intercept。"""

    src0: float
    src1: float
    slope: float
    intercept: float


def _build_segments(anchors: list[TimeAnchor]) -> list[_Segment]:
    segments: list[_Segment] = []
    for left, right in zip(anchors, anchors[1:]):
        span = right.source - left.source  # 严格递增保证 span > 0
        slope = (right.target - left.target) / span
        segments.append(
            _Segment(
                src0=left.source,
                src1=right.source,
                slope=slope,
                intercept=left.target - slope * left.source,
            )
        )
    return segments


def map_time(t: float, segments: list[_Segment]) -> float:
    """把单个源时间点按锚点分段线性映射到目标时间轴。

    锚点位置精确命中时返回对应 target；超出首尾时沿最近区间外推。
    """
    # 右侧最近区间：命中 source_k 时归入区间 k-1，其左端点即为该锚点，
    # 插值结果恰好等于 target_k；t < src0 时回落到第 0 段（外推）。
    sources = [seg.src1 for seg in segments]
    idx = bisect_right(sources, t)
    if idx >= len(segments):
        idx = len(segments) - 1  # t 晚于末锚点：沿末区间外推
    seg = segments[idx]
    return seg.slope * t + seg.intercept


@dataclass
class RetimeResult:
    """retime_cues 的结果。

    cue_ids 非空时表示存在负时间或非法区间，此时 retimed 不可用；
    否则 retimed 为保持输入顺序的完整校时列表。
    """

    retimed: list[Cue] = field(default_factory=list)
    cue_ids: list[str] = field(default_factory=list)


def retime_cues(cues: list[Cue], anchors: list[TimeAnchor]) -> RetimeResult:
    """对每条 cue 的 start/end 分别做锚点映射。

    id、text、speaker 与输入顺序保持不变。任一映射时间为负或映射后
    end <= start 时，收集全部相关 cue id（去重、保持输入顺序），
    不返回任何 cue。
    """
    segments = _build_segments(anchors)

    bad_ids: list[str] = []
    seen_bad: set[str] = set()
    mapped: list[tuple[Cue, float, float]] = []

    for cue in cues:
        new_start = map_time(cue.start, segments)
        new_end = map_time(cue.end, segments)
        mapped.append((cue, new_start, new_end))
        if new_start < 0 or new_end < 0 or new_end <= new_start:
            if cue.id not in seen_bad:
                seen_bad.add(cue.id)
                bad_ids.append(cue.id)

    if bad_ids:
        return RetimeResult(cue_ids=bad_ids)

    retimed = [
        Cue(
            id=cue.id,
            start=new_start,
            end=new_end,
            text=cue.text,
            speaker=cue.speaker,
        )
        for cue, new_start, new_end in mapped
    ]
    return RetimeResult(retimed=retimed)
