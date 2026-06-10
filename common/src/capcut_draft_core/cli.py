"""命令行入口：把整条 ASR → 切点 → 草稿流水线串起来。

支持单视频和批量（--main 指向目录时自动遍历）。
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Callable, Optional

from .asr import transcribe
from .builder import build_draft
from .cutter import select_cut_points


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _list_videos(folder: Path) -> list[Path]:
    exts = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in exts)


def _process_one(
    main_path: Path,
    brolls: list[Path],
    out_dir: Path,
    draft_name: str,
    *,
    pause_threshold: float,
    min_cut_interval: float,
    max_cuts: int | None,
    broll_duration: float,
    width: int,
    height: int,
    fps: float,
    add_subtitles: bool,
    skip_asr: bool,
    log: logging.Logger,
    progress_cb: Optional[callable] = None,  # type: ignore[name-defined]
) -> str:
    """处理单个主视频，返回草稿路径。

    progress_cb(pct: int, msg: str) — 客户端 worker 可选传，
    用来向服务端上报进度；不传则静默。
    """
    segments = []
    cuts = []
    total_dur = None

    def _report(pct: int, msg: str) -> None:
        log.info("[%d%%] %s", pct, msg)
        if progress_cb is not None:
            try:
                progress_cb(pct, msg)
            except Exception as e:  # 不让回调报错炸掉主流程
                log.debug("progress_cb 回调异常: %s", e)

    _report(2, "开始处理")

    if not skip_asr:
        _report(5, f"ASR 转写中: {main_path.name}")
        result = transcribe(str(main_path), pause_threshold=pause_threshold)
        segments = result.segments
        cuts = result.cut_points
        log.info("[%s] 字幕段: %d, 停顿切点: %d",
                 main_path.name, len(segments), len(cuts))
        for s in segments[:5]:
            log.info("  [%.2f-%.2f] %s", s.start, s.end, s.text)
        _report(40, f"ASR 完成（{len(segments)} 段，{len(cuts)} 切点）")
    else:
        from .builder import _probe_video_duration
        try:
            total_dur = _probe_video_duration(str(main_path))
        except Exception as e:
            log.error("[%s] 无法探测时长: %s", main_path.name, e)
            raise
        _report(15, f"跳过 ASR，主时长 {total_dur:.1f}s")

    cuts = select_cut_points(
        cuts,
        min_interval=min_cut_interval,
        max_cuts=max_cuts,
        total_duration=total_dur,
        fallback_interval=6.0,
    )
    log.info("[%s] 最终切点数: %d", main_path.name, len(cuts))
    _report(50, f"切点确定: {len(cuts)} 个")

    _report(55, f"开始构建草稿（{len(brolls)} 个 B-roll）")
    out = build_draft(
        main_video=str(main_path),
        broll_clips=[str(p) for p in brolls],
        segments=segments,
        cut_points=cuts,
        broll_duration=broll_duration,
        out_dir=str(out_dir),
        draft_name=draft_name,
        width=width,
        height=height,
        fps=fps,
        add_subtitles=add_subtitles,
    )
    _report(98, f"草稿构建完成")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="capcut-draft",
        description="数字人视频 + B-roll + ASR 字幕 → 剪映草稿（支持单视频或批量）",
    )
    parser.add_argument("--main", required=True,
                        help="主视频（数字人/口播）路径，或数字人视频目录（批量）")
    parser.add_argument("--broll", required=True, help="B-roll 素材文件夹路径")
    parser.add_argument("--out", default="outputs", help="输出目录（默认 ./outputs）")
    parser.add_argument("--name", default="AI合成",
                        help="单视频模式下的草稿名；批量模式下作为前缀（{name}=视频文件名）")
    parser.add_argument("--name-template", default="{prefix}_{name}",
                        help="批量模式下草稿名模板，可使用 {prefix} 和 {name}（视频名）")
    parser.add_argument("--width", type=int, default=1080)
    parser.add_argument("--height", type=int, default=1920)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--pause-threshold", type=float, default=0.6,
                        help="判定为语义停顿的最短静音长度（秒）")
    parser.add_argument("--min-cut-interval", type=float, default=2.5,
                        help="相邻切点最小间隔（秒）")
    parser.add_argument("--max-cuts", type=int, default=None,
                        help="最多使用多少个切点")
    parser.add_argument("--broll-duration", type=float, default=2.5,
                        help="每个 B-roll 在画面上停留的时长（秒）")
    parser.add_argument("--no-subtitles", action="store_true", help="不写字幕轨")
    parser.add_argument("--skip-asr", action="store_true",
                        help="跳过 ASR（仅基于固定间隔切点）")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    log = logging.getLogger("capcut-draft")

    main_path = Path(args.main)
    broll_dir = Path(args.broll)
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not main_path.exists():
        log.error("主视频/目录不存在: %s", main_path)
        return 2
    if not broll_dir.exists() or not broll_dir.is_dir():
        log.error("B-roll 目录不存在: %s", broll_dir)
        return 2

    brolls = _list_videos(broll_dir)
    if not brolls:
        log.error("B-roll 目录里没有视频文件: %s", broll_dir)
        return 2
    log.info("找到 %d 个 B-roll 素材", len(brolls))

    # 决定模式：单视频 or 批量
    if main_path.is_dir():
        mains = _list_videos(main_path)
        if not mains:
            log.error("主目录里没有视频文件: %s", main_path)
            return 2
        log.info("批量模式: %d 个主视频", len(mains))
        results: list[str] = []
        for i, m in enumerate(mains, 1):
            log.info("=" * 60)
            log.info("[%d/%d] 处理: %s", i, len(mains), m.name)
            draft_name = args.name_template.format(prefix=args.name, name=m.stem)
            try:
                p = _process_one(
                    m, brolls, out_dir, draft_name,
                    pause_threshold=args.pause_threshold,
                    min_cut_interval=args.min_cut_interval,
                    max_cuts=args.max_cuts,
                    broll_duration=args.broll_duration,
                    width=args.width,
                    height=args.height,
                    fps=args.fps,
                    add_subtitles=not args.no_subtitles,
                    skip_asr=args.skip_asr,
                    log=log,
                )
                results.append(p)
            except Exception as e:
                log.error("处理 %s 失败: %s", m.name, e)
                continue
        log.info("=" * 60)
        log.info("批量完成，共 %d 个草稿，输出在 %s", len(results), out_dir)
        for p in results:
            log.info("  - %s", p)
    else:
        draft_path = _process_one(
            main_path, brolls, out_dir, args.name,
            pause_threshold=args.pause_threshold,
            min_cut_interval=args.min_cut_interval,
            max_cuts=args.max_cuts,
            broll_duration=args.broll_duration,
            width=args.width,
            height=args.height,
            fps=args.fps,
            add_subtitles=not args.no_subtitles,
            skip_asr=args.skip_asr,
            log=log,
        )
        log.info("完成。草稿目录: %s", draft_path)
        log.info("打开剪映 → 媒体 → 导入 → 选择该文件夹即可。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
