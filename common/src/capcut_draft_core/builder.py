"""剪映草稿生成：使用 pyJianYingDraft 0.2.x 拼出 .draft 文件夹。"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .models import CutPoint, Segment

log = logging.getLogger(__name__)

# 默认剪映草稿画布尺寸（竖屏 1080x1920），主流口播配置
DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1920
DEFAULT_FPS = 30


def _probe_video_duration(path: str) -> float:
    """用 imageio-ffmpeg 探测视频时长（秒）。"""
    import re
    import subprocess

    import imageio_ffmpeg

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    # ffmpeg 把信息输出到 stderr，且只要没指定输出文件就会返回非 0，所以用 check=False
    proc = subprocess.run(
        [ffmpeg_exe, "-i", path],
        capture_output=True,
        text=True,
        check=False,
    )
    out = (proc.stderr or "") + (proc.stdout or "")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", out)
    if not m:
        raise RuntimeError(f"无法解析视频时长: {path}\n{out[:300]}")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def build_draft(
    *,
    main_video: str,
    broll_clips: list[str],
    segments: list[Segment],
    cut_points: list[CutPoint],
    broll_duration: float = 2.5,
    out_dir: str,
    draft_name: str = "AI合成",
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    fps: int = DEFAULT_FPS,
    add_subtitles: bool = True,
) -> str:
    """组装剪映草稿并写出到 out_dir/draft_name。

    返回草稿文件夹绝对路径。
    """
    import pyJianYingDraft as draft
    from pyJianYingDraft import (
        DraftFolder,
        TextStyle,
        TrackType,
        VideoMaterial,
        VideoSegment,
    )

    main_duration = _probe_video_duration(main_video)
    log.info(
        "主轨时长: %.2fs, 切点数: %d, B-roll 数: %d",
        main_duration, len(cut_points), len(broll_clips),
    )

    # 准备输出目录
    out_dir_abs = Path(out_dir).resolve()
    out_dir_abs.mkdir(parents=True, exist_ok=True)
    draft_path = out_dir_abs / draft_name

    # pyJianYingDraft 的 create_draft 内部会 rmtree 整个 draft_path，
    # 所以先把素材 stage 到一个旁路缓存目录，等 create_draft 之后再搬进 assets。
    stage_dir = out_dir_abs / f".{draft_name}_stage"
    if stage_dir.exists():
        shutil.rmtree(stage_dir, ignore_errors=True)
    stage_dir.mkdir(parents=True, exist_ok=True)

    def _stage(src: str) -> str:
        dst = stage_dir / Path(src).name
        if dst.exists():
            try:
                if dst.resolve() == Path(src).resolve():
                    return str(dst)
            except Exception:
                pass
        shutil.copy2(src, dst)
        return str(dst)

    main_local = _stage(main_video)
    broll_locals = [_stage(p) for p in broll_clips]
    log.info("已 stage 素材到: %s", stage_dir)

    # 用 DraftFolder 创建草稿（allow_replace=True 内部会清空 draft_path）
    folder = DraftFolder(str(out_dir_abs))
    script = folder.create_draft(
        draft_name=draft_name,
        width=width,
        height=height,
        fps=fps,
        maintrack_adsorb=True,
        allow_replace=True,
    )

    # 加轨道：主视频轨、B-roll 视频轨（在上层）、文字轨
    main_track = "main_video"
    broll_track = "broll_overlay"
    text_track = "subtitle"
    script.add_track(TrackType.video, track_name=main_track)
    if broll_locals and cut_points:
        script.add_track(TrackType.video, track_name=broll_track)
    if add_subtitles and segments:
        script.add_track(TrackType.text, track_name=text_track)

    # 注册主视频素材并铺满整段
    main_mat = VideoMaterial(main_local)
    script.add_material(main_mat)
    main_seg = VideoSegment(main_mat, target_timerange=draft.trange(0, main_duration))
    script.add_segment(main_seg, track_name=main_track)

    # B-roll：在每个切点位置插一段
    if broll_locals and cut_points:
        for i, cp in enumerate(cut_points):
            clip = broll_locals[i % len(broll_locals)]
            remaining = main_duration - cp.time
            if remaining < 0.3:
                continue
            try:
                clip_dur = _probe_video_duration(clip)
            except Exception:
                clip_dur = broll_duration
            use_dur = min(broll_duration, clip_dur, remaining)
            if use_dur < 0.3:
                continue
            broll_mat = VideoMaterial(clip)
            script.add_material(broll_mat)
            seg = VideoSegment(
                broll_mat,
                target_timerange=draft.trange(cp.time, use_dur),
            )
            script.add_segment(seg, track_name=broll_track)

    # 字幕：按 ASR 分段加文字
    if add_subtitles and segments:
        style = TextStyle(
            size=10.0,
            color=(1.0, 1.0, 1.0),
            align=1,  # 居中
            auto_wrapping=True,
        )
        for seg in segments:
            if not seg.text:
                continue
            duration = max(0.3, seg.duration)
            ts = draft.TextSegment(
                text=seg.text,
                timerange=draft.trange(seg.start, duration),
                style=style,
            )
            script.add_segment(ts, track_name=text_track)

    script.save()

    # 把 stage 里的素材搬到草稿的 assets 子目录里（draft_content.json 引用此路径）
    assets_dir = draft_path / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    for src in [main_local, *broll_locals]:
        dst = assets_dir / Path(src).name
        if dst.exists():
            try:
                if dst.resolve() == Path(src).resolve():
                    continue
            except Exception:
                pass
        shutil.copy2(src, dst)
    shutil.rmtree(stage_dir, ignore_errors=True)
    log.info("草稿已生成: %s", draft_path)
    return str(draft_path)
