"""ASR + VAD 转写：使用 funasr 识别音频并返回带时间戳的分段与停顿点。"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .models import CutPoint, Segment, Word

log = logging.getLogger(__name__)

# 默认模型：paraformer-zh（中文识别）+ fsmn-vad（语音活动检测）+ ct-punc（标点恢复）
DEFAULT_ASR_MODEL = "paraformer-zh"
DEFAULT_VAD_MODEL = "fsmn-vad"
DEFAULT_PUNC_MODEL = "ct-punc"


def _extract_audio_to_wav(video_path: str, wav_path: str, sample_rate: int = 16000) -> None:
    """从视频中提取 16k 单声道 wav；优先用 imageio-ffmpeg 自带二进制，无系统 ffmpeg 也能跑。"""
    import imageio_ffmpeg

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg_exe, "-y", "-i", video_path,
        "-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "wav", wav_path,
    ]
    log.info("提取音频: %s", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 提取音频失败，退出码 {proc.returncode}\n"
            f"stderr: {proc.stderr[-500:]}"
        )


def _load_audio_wav(wav_path: str, sample_rate: int = 16000) -> np.ndarray:
    """读取 wav 为 float32 单声道 numpy。"""
    import soundfile as sf

    audio, sr = sf.read(wav_path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != sample_rate:
        # 简单线性重采样；funasr 内部也会再处理，这里保证采样率匹配即可
        import scipy.signal as signal

        audio = signal.resample(audio, int(len(audio) * sample_rate / sr))
    return audio.astype("float32")


@dataclass
class TranscribeResult:
    segments: list[Segment]
    cut_points: list[CutPoint]  # 来自 VAD 检测到的停顿中点


def transcribe(
    video_path: str,
    *,
    pause_threshold: float = 0.6,
    min_segment_duration: float = 0.3,
    asr_model: str = DEFAULT_ASR_MODEL,
    vad_model: str = DEFAULT_VAD_MODEL,
    punc_model: str | None = DEFAULT_PUNC_MODEL,
    cache_dir: str | None = None,
) -> TranscribeResult:
    """对视频做 ASR + VAD，返回分段和停顿切点。

    pause_threshold: 判定为"语义停顿"的最小静音长度（秒）
    min_segment_duration: 过滤掉短于该时长的段
    """
    from funasr import AutoModel  # 延迟导入，避免冷启动慢

    video_p = Path(video_path)
    if cache_dir is None:
        cache_dir = str(video_p.with_suffix(".wav"))
    wav_path = cache_dir if cache_dir.endswith(".wav") else str(Path(cache_dir) / "audio.wav")
    Path(wav_path).parent.mkdir(parents=True, exist_ok=True)

    if not Path(wav_path).exists() or Path(wav_path).stat().st_size == 0:
        _extract_audio_to_wav(str(video_p), wav_path)

    log.info("加载 funasr 模型: %s / %s / %s", asr_model, vad_model, punc_model)
    model = AutoModel(
        model=asr_model,
        vad_model=vad_model,
        punc_model=punc_model,
        disable_update=True,
    )

    log.info("开始转写: %s", wav_path)
    result = model.generate(
        input=wav_path,
        batch_size_s=300,
        is_final=True,
    )

    segments: list[Segment] = []
    cut_points: list[CutPoint] = []

    for item in result:
        if "sentence_info" in item and item["sentence_info"]:
            for s in item["sentence_info"]:
                text = (s.get("text") or "").strip()
                start = float(s.get("start", 0)) / 1000.0
                end = float(s.get("end", 0)) / 1000.0
                if not text or (end - start) < min_segment_duration:
                    continue
                words_raw = s.get("word_list") or []
                words = [
                    Word(text=w.get("word", ""), start=float(w.get("start", 0)) / 1000.0,
                         end=float(w.get("end", 0)) / 1000.0)
                    for w in words_raw if w.get("word")
                ]
                segments.append(Segment(text=text, start=start, end=end, words=words))
        elif "timestamp" in item and item["timestamp"]:
            # 无标点时按时间戳切句
            text = (item.get("text") or "").strip()
            if not text:
                continue
            ts = item["timestamp"]
            # ts 形如 [[0, 500], [500, 1200], ...] 毫秒
            for i, (s_ms, e_ms) in enumerate(ts):
                if i == 0:
                    seg_start = s_ms / 1000.0
                seg_end = e_ms / 1000.0
            start = ts[0][0] / 1000.0
            end = ts[-1][1] / 1000.0
            if (end - start) >= min_segment_duration:
                segments.append(Segment(text=text, start=start, end=end))

        # 收集 VAD 停顿切点
        vad_segments = item.get("vad_segs") or []
        for i in range(len(vad_segments) - 1):
            cur = vad_segments[i]
            nxt = vad_segments[i + 1]
            gap = (nxt[0] - cur[1]) / 1000.0
            if gap >= pause_threshold:
                mid = (cur[1] + nxt[0]) / 2000.0
                cut_points.append(CutPoint(time=mid, reason=f"pause:{gap:.2f}s"))

    segments.sort(key=lambda x: x.start)
    cut_points.sort(key=lambda x: x.time)
    log.info("转写完成: %d 段, %d 个停顿切点", len(segments), len(cut_points))
    return TranscribeResult(segments=segments, cut_points=cut_points)
