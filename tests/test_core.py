"""测试 capcut_draft_core 三个核心模块：

- cutter.py: 切点策略（纯函数，无需 mock）
  空列表 / fallback / 间隔过滤 / pause 替换 / max_cuts 采样 / max_cuts=0 / 全部过近
- asr.py: ASR 转写（mock funasr / ffmpeg / soundfile）
  ffmpeg 失败 / 文件不存在 / 正常转写 / timestamp 回退 / 无停顿 / 有停顿
  pause_threshold 边界 / min_segment_duration 过滤 / WAV 缓存命中
- builder.py: 草稿组装（mock pyJianYingDraft / _probe_video_duration / shutil）
  正常构建 / 无 B-roll / 无切点 / 无字幕 / 空 segments
  B-roll 循环 / B-roll 太短 / 切点越界 / 探测失败 / 空文本段
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from capcut_draft_core.models import CutPoint, Segment, Word


# ============================================================
#  cutter.py 测试（纯函数，无需 mock）
# ============================================================


def test_cutter_empty_no_duration():
    """空列表 + total_duration=None → 返回 []"""
    from capcut_draft_core import cutter

    assert cutter.select_cut_points([]) == []


def test_cutter_empty_with_duration():
    """空列表 + total_duration=10, fallback_interval=6 → 生成均匀切点 [CutPoint(6, "fixed")]"""
    from capcut_draft_core import cutter

    result = cutter.select_cut_points([], total_duration=10, fallback_interval=6)
    assert result == [CutPoint(time=6.0, reason="fixed")]


def test_cutter_empty_short_duration():
    """空列表 + total_duration=5, fallback_interval=6 → 返回 []（while t < total_duration 不满足）"""
    from capcut_draft_core import cutter

    assert cutter.select_cut_points([], total_duration=5, fallback_interval=6) == []


def test_cutter_single_point():
    """单个切点 → 保留"""
    from capcut_draft_core import cutter

    result = cutter.select_cut_points([CutPoint(time=3.0, reason="pause:0.8s")])
    assert len(result) == 1
    assert result[0] == CutPoint(time=3.0, reason="pause:0.8s")


def test_cutter_multiple_sufficient_interval():
    """多个间隔足够的切点（间隔 > min_interval）→ 全部保留"""
    from capcut_draft_core import cutter

    cuts = [
        CutPoint(time=2.0, reason="pause"),
        CutPoint(time=5.0, reason="pause"),
        CutPoint(time=8.5, reason="pause"),
    ]
    result = cutter.select_cut_points(cuts, min_interval=2.5)
    assert len(result) == 3
    assert [c.time for c in result] == [2.0, 5.0, 8.5]


def test_cutter_too_close_points():
    """间隔过近的切点（间隔 < min_interval）→ 只保留第一个和足够远的"""
    from capcut_draft_core import cutter

    cuts = [
        CutPoint(time=2.0, reason="fixed"),
        CutPoint(time=3.5, reason="fixed"),  # 间隔 1.5 < 2.5, 非 pause → 丢弃
        CutPoint(time=6.0, reason="fixed"),  # 间隔 4.0 >= 2.5
    ]
    result = cutter.select_cut_points(cuts, min_interval=2.5)
    assert len(result) == 2
    assert [c.time for c in result] == [2.0, 6.0]


def test_cutter_pause_replaces_previous():
    """更长停顿替换：reason 以 "pause" 开头 + 间隔 >= 0.5s → 替换前一个"""
    from capcut_draft_core import cutter

    cuts = [
        CutPoint(time=2.0, reason="fixed"),
        CutPoint(time=2.8, reason="pause:1.0s"),  # 间隔 0.8 >= 0.5
    ]
    result = cutter.select_cut_points(cuts, min_interval=2.5)
    assert len(result) == 1
    assert result[0].time == 2.8
    assert result[0].reason == "pause:1.0s"


def test_cutter_pause_no_replace_too_close():
    """pause 切点间隔 < 0.5s → 不替换，直接丢弃"""
    from capcut_draft_core import cutter

    cuts = [
        CutPoint(time=2.0, reason="fixed"),
        CutPoint(time=2.3, reason="pause:0.5s"),  # 间隔 0.3 < 0.5
    ]
    result = cutter.select_cut_points(cuts, min_interval=2.5)
    assert len(result) == 1
    assert result[0].time == 2.0  # pause 间隔不足 0.5，不替换


def test_cutter_max_cuts_sampling():
    """max_cuts 限制：10 个切点, max_cuts=3 → 均匀采样 3 个"""
    from capcut_draft_core import cutter

    cuts = [CutPoint(time=float(i), reason="pause") for i in range(1, 11)]
    result = cutter.select_cut_points(cuts, min_interval=0.5, max_cuts=3)
    assert len(result) == 3
    # step = 10/3 ≈ 3.333; indices: 0→1.0, 3→4.0, 6→7.0
    assert [c.time for c in result] == [1.0, 4.0, 7.0]


def test_cutter_max_cuts_zero():
    """max_cuts=0 → 返回 []"""
    from capcut_draft_core import cutter

    result = cutter.select_cut_points(
        [CutPoint(time=3.0, reason="pause")], max_cuts=0
    )
    assert result == []


def test_cutter_all_too_close():
    """所有切点间隔都小于 min_interval → 仅保留第一个"""
    from capcut_draft_core import cutter

    cuts = [
        CutPoint(time=1.0, reason="fixed"),
        CutPoint(time=1.5, reason="fixed"),
        CutPoint(time=2.0, reason="fixed"),
    ]
    result = cutter.select_cut_points(cuts, min_interval=2.5)
    assert len(result) == 1
    assert result[0].time == 1.0


# ============================================================
#  asr.py 测试（mock funasr / ffmpeg / soundfile）
# ============================================================


@pytest.fixture
def asr_mock_modules():
    """注入 mock funasr / imageio_ffmpeg 到 sys.modules。"""
    mock_funasr = MagicMock()
    mock_imageio = MagicMock()
    mock_imageio.get_ffmpeg_exe.return_value = "/fake/ffmpeg"
    with patch.dict(
        sys.modules,
        {"funasr": mock_funasr, "imageio_ffmpeg": mock_imageio},
    ):
        yield {"funasr": mock_funasr, "imageio_ffmpeg": mock_imageio}


def _funasr_item(sentence_info=None, timestamp=None, text="测试文本", vad_segs=None):
    """构造一条 funasr generate() 返回 dict。"""
    item = {"text": text}
    if sentence_info is not None:
        item["sentence_info"] = sentence_info
    if timestamp is not None:
        item["timestamp"] = timestamp
    if vad_segs is not None:
        item["vad_segs"] = vad_segs
    return item


def _fake_wav(tmp_path, name="test.wav", size=256):
    """在 tmp_path 下创建一个非空假 wav 文件并返回 Path。"""
    wav = tmp_path / name
    wav.write_bytes(b"RIFF" + b"\x00" * size)
    return wav


# ---- 场景 1: ffmpeg 失败 ----


def test_asr_ffmpeg_failure(asr_mock_modules):
    """subprocess.run 返回 returncode=1 → 抛出 RuntimeError"""
    from capcut_draft_core import asr

    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stderr = "No such file"

    with patch("capcut_draft_core.asr.subprocess.run", return_value=mock_proc):
        with pytest.raises(RuntimeError, match="ffmpeg 提取音频失败"):
            asr._extract_audio_to_wav("/fake/video.mp4", "/fake/out.wav")


# ---- 场景 2: 文件不存在 → ffmpeg 失败 ----


def test_asr_file_not_exists(tmp_path, asr_mock_modules):
    """视频文件不存在 → ffmpeg 失败 → RuntimeError 从 transcribe 传播"""
    from capcut_draft_core import asr

    video = tmp_path / "nonexistent.mp4"
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stderr = "No such file"

    with patch("capcut_draft_core.asr.subprocess.run", return_value=mock_proc):
        with pytest.raises(RuntimeError, match="ffmpeg 提取音频失败"):
            asr.transcribe(str(video))


# ---- 场景 3: 正常转写 ----


def test_asr_normal_transcribe(tmp_path, asr_mock_modules):
    """funasr 返回 sentence_info → 正确解析 segments 和 cut_points"""
    from capcut_draft_core import asr

    video = tmp_path / "test.mp4"
    video.write_text("fake")
    _fake_wav(tmp_path, "test.wav")

    mock_model = MagicMock()
    mock_model.generate.return_value = [
        _funasr_item(
            sentence_info=[
                {
                    "text": "你好世界",
                    "start": 0,
                    "end": 1500,
                    "word_list": [
                        {"word": "你好", "start": 0, "end": 600},
                        {"word": "世界", "start": 600, "end": 1500},
                    ],
                },
                {"text": "再见", "start": 2000, "end": 2800, "word_list": []},
            ],
            vad_segs=[[0, 1500], [2000, 2800]],  # gap=0.5s < 0.6 → 无切点
        )
    ]
    asr_mock_modules["funasr"].AutoModel.return_value = mock_model

    result = asr.transcribe(str(video), pause_threshold=0.6)
    assert len(result.segments) == 2
    assert result.segments[0].text == "你好世界"
    assert result.segments[0].start == 0.0
    assert result.segments[0].end == 1.5
    assert len(result.segments[0].words) == 2
    assert result.segments[1].text == "再见"
    assert len(result.cut_points) == 0  # 0.5s gap < 0.6s threshold


# ---- 场景 4: 无 sentence_info 但有 timestamp ----


def test_asr_timestamp_only(tmp_path, asr_mock_modules):
    """无 sentence_info 但有 timestamp → 按时间戳解析"""
    from capcut_draft_core import asr

    video = tmp_path / "test.mp4"
    video.write_text("fake")
    _fake_wav(tmp_path, "test.wav")

    mock_model = MagicMock()
    mock_model.generate.return_value = [
        _funasr_item(
            timestamp=[[0, 2000], [2000, 4500]],
            text="整段文本",
            vad_segs=[],
        )
    ]
    asr_mock_modules["funasr"].AutoModel.return_value = mock_model

    result = asr.transcribe(str(video), min_segment_duration=0.3)
    assert len(result.segments) == 1
    assert result.segments[0].text == "整段文本"
    assert result.segments[0].start == 0.0
    assert result.segments[0].end == 4.5


# ---- 场景 5: 无停顿 ----


def test_asr_no_pause(tmp_path, asr_mock_modules):
    """VAD 返回连续语音段 → cut_points 为空"""
    from capcut_draft_core import asr

    video = tmp_path / "test.mp4"
    video.write_text("fake")
    _fake_wav(tmp_path, "test.wav")

    mock_model = MagicMock()
    mock_model.generate.return_value = [
        _funasr_item(
            sentence_info=[{"text": "连续说话", "start": 0, "end": 5000}],
            vad_segs=[[0, 5000]],  # 单段，无间隔
        )
    ]
    asr_mock_modules["funasr"].AutoModel.return_value = mock_model

    result = asr.transcribe(str(video), pause_threshold=0.6)
    assert len(result.cut_points) == 0


# ---- 场景 6: 有停顿 ----


def test_asr_with_pause(tmp_path, asr_mock_modules):
    """VAD 返回有间隔的段 → 正确计算切点位置和 reason"""
    from capcut_draft_core import asr

    video = tmp_path / "test.mp4"
    video.write_text("fake")
    _fake_wav(tmp_path, "test.wav")

    mock_model = MagicMock()
    mock_model.generate.return_value = [
        _funasr_item(
            sentence_info=[
                {"text": "第一段", "start": 0, "end": 3000},
                {"text": "第二段", "start": 4000, "end": 7000},
            ],
            vad_segs=[[0, 3000], [4000, 7000]],  # gap=1.0s
        )
    ]
    asr_mock_modules["funasr"].AutoModel.return_value = mock_model

    result = asr.transcribe(str(video), pause_threshold=0.6)
    assert len(result.cut_points) == 1
    # mid = (3000 + 4000) / 2000 = 3.5
    assert result.cut_points[0].time == 3.5
    assert result.cut_points[0].reason.startswith("pause:")
    assert "1.00s" in result.cut_points[0].reason


# ---- 场景 7: pause_threshold 边界 ----


def test_asr_pause_threshold_boundary(tmp_path, asr_mock_modules):
    """停顿刚好等于 pause_threshold → 应该被保留"""
    from capcut_draft_core import asr

    video = tmp_path / "test.mp4"
    video.write_text("fake")
    _fake_wav(tmp_path, "test.wav")

    mock_model = MagicMock()
    mock_model.generate.return_value = [
        _funasr_item(
            sentence_info=[
                {"text": "第一段", "start": 0, "end": 3000},
                {"text": "第二段", "start": 3600, "end": 6000},
            ],
            vad_segs=[[0, 3000], [3600, 6000]],  # gap 恰好 0.6s
        )
    ]
    asr_mock_modules["funasr"].AutoModel.return_value = mock_model

    result = asr.transcribe(str(video), pause_threshold=0.6)
    assert len(result.cut_points) == 1  # gap >= threshold → 保留


# ---- 场景 8: min_segment_duration 过滤 ----


def test_asr_min_segment_duration_filter(tmp_path, asr_mock_modules):
    """短于 min_segment_duration 的段 → 被过滤"""
    from capcut_draft_core import asr

    video = tmp_path / "test.mp4"
    video.write_text("fake")
    _fake_wav(tmp_path, "test.wav")

    mock_model = MagicMock()
    mock_model.generate.return_value = [
        _funasr_item(
            sentence_info=[
                {"text": "短段", "start": 0, "end": 200, "word_list": []},  # 0.2s
                {"text": "正常段", "start": 500, "end": 2000, "word_list": []},  # 1.5s
            ],
            vad_segs=[[0, 200], [500, 2000]],
        )
    ]
    asr_mock_modules["funasr"].AutoModel.return_value = mock_model

    result = asr.transcribe(str(video), min_segment_duration=0.3)
    assert len(result.segments) == 1
    assert result.segments[0].text == "正常段"


# ---- 场景 9: WAV 缓存命中 ----


def test_asr_wav_cache_hit(tmp_path, asr_mock_modules):
    """wav 文件已存在 → 跳过提取步骤"""
    from capcut_draft_core import asr

    video = tmp_path / "test.mp4"
    video.write_text("fake")
    _fake_wav(tmp_path, "test.wav")

    mock_model = MagicMock()
    mock_model.generate.return_value = [
        _funasr_item(
            sentence_info=[{"text": "缓存命中", "start": 0, "end": 1000}],
            vad_segs=[],
        )
    ]
    asr_mock_modules["funasr"].AutoModel.return_value = mock_model

    with patch("capcut_draft_core.asr._extract_audio_to_wav") as mock_extract:
        result = asr.transcribe(str(video))
        mock_extract.assert_not_called()
    assert len(result.segments) == 1


# ============================================================
#  builder.py 测试（mock pyJianYingDraft / _probe_video_duration / shutil）
# ============================================================


@pytest.fixture
def mock_pyjyd():
    """注入 mock pyJianYingDraft 到 sys.modules，返回关键 mock 对象。"""
    mock_mod = MagicMock()
    # TrackType 枚举模拟
    mock_mod.TrackType.video = "video"
    mock_mod.TrackType.text = "text"
    # trange 函数模拟：返回 (start, duration) 元组
    mock_mod.trange = MagicMock(side_effect=lambda s, d: (s, d))
    # DraftFolder → create_draft → script
    mock_folder_inst = MagicMock()
    mock_script = MagicMock()
    mock_folder_inst.create_draft.return_value = mock_script
    mock_mod.DraftFolder.return_value = mock_folder_inst

    with patch.dict(sys.modules, {"pyJianYingDraft": mock_mod}):
        yield {"module": mock_mod, "script": mock_script, "folder": mock_folder_inst}


def _builder_kw(tmp_path, **overrides):
    """build_draft 默认参数（配合 mock 使用）。"""
    kw = dict(
        main_video=str(tmp_path / "main.mp4"),
        broll_clips=[str(tmp_path / "broll1.mp4")],
        segments=[Segment(text="你好", start=0.0, end=1.5)],
        cut_points=[CutPoint(time=2.0, reason="pause:1.0s")],
        broll_duration=2.5,
        out_dir=str(tmp_path / "output"),
        draft_name="AI合成",
        add_subtitles=True,
    )
    kw.update(overrides)
    return kw


# ---- 场景 1: 正常构建 ----


def test_builder_normal(tmp_path, mock_pyjyd):
    """主视频 + B-roll + segments + cuts → 验证各轨道被添加"""
    from capcut_draft_core import builder

    script = mock_pyjyd["script"]
    kwargs = _builder_kw(tmp_path)
    with patch.object(builder, "_probe_video_duration", return_value=30.0), \
         patch.object(builder.shutil, "copy2"), \
         patch.object(builder.shutil, "rmtree"):
        result = builder.build_draft(**kwargs)

    assert isinstance(result, str)
    track_names = [c.kwargs["track_name"] for c in script.add_track.call_args_list]
    assert "main_video" in track_names
    assert "broll_overlay" in track_names
    assert "subtitle" in track_names
    assert script.add_material.call_count == 2
    assert script.add_segment.call_count == 3


# ---- 场景 2: 无 B-roll ----


def test_builder_no_broll(tmp_path, mock_pyjyd):
    """broll_clips=[] → 不添加 B-roll 轨道"""
    from capcut_draft_core import builder

    script = mock_pyjyd["script"]
    kwargs = _builder_kw(tmp_path, broll_clips=[])
    with patch.object(builder, "_probe_video_duration", return_value=30.0), \
         patch.object(builder.shutil, "copy2"), \
         patch.object(builder.shutil, "rmtree"):
        builder.build_draft(**kwargs)

    track_names = [c.kwargs["track_name"] for c in script.add_track.call_args_list]
    assert "broll_overlay" not in track_names


# ---- 场景 3: 无切点 ----


def test_builder_no_cut_points(tmp_path, mock_pyjyd):
    """cut_points=[] → 不添加 B-roll 轨道"""
    from capcut_draft_core import builder

    script = mock_pyjyd["script"]
    kwargs = _builder_kw(tmp_path, cut_points=[])
    with patch.object(builder, "_probe_video_duration", return_value=30.0), \
         patch.object(builder.shutil, "copy2"), \
         patch.object(builder.shutil, "rmtree"):
        builder.build_draft(**kwargs)

    track_names = [c.kwargs["track_name"] for c in script.add_track.call_args_list]
    assert "broll_overlay" not in track_names


# ---- 场景 4: 无字幕 ----


def test_builder_no_subtitles(tmp_path, mock_pyjyd):
    """add_subtitles=False → 不添加文字轨道"""
    from capcut_draft_core import builder

    script = mock_pyjyd["script"]
    kwargs = _builder_kw(tmp_path, add_subtitles=False)
    with patch.object(builder, "_probe_video_duration", return_value=30.0), \
         patch.object(builder.shutil, "copy2"), \
         patch.object(builder.shutil, "rmtree"):
        builder.build_draft(**kwargs)

    track_names = [c.kwargs["track_name"] for c in script.add_track.call_args_list]
    assert "subtitle" not in track_names


# ---- 场景 5: 空 segments ----


def test_builder_empty_segments(tmp_path, mock_pyjyd):
    """segments=[] → 不添加文字轨道"""
    from capcut_draft_core import builder

    script = mock_pyjyd["script"]
    kwargs = _builder_kw(tmp_path, segments=[])
    with patch.object(builder, "_probe_video_duration", return_value=30.0), \
         patch.object(builder.shutil, "copy2"), \
         patch.object(builder.shutil, "rmtree"):
        builder.build_draft(**kwargs)

    track_names = [c.kwargs["track_name"] for c in script.add_track.call_args_list]
    assert "subtitle" not in track_names


# ---- 场景 6: B-roll 循环利用 ----


def test_builder_broll_reuse(tmp_path, mock_pyjyd):
    """1 个 B-roll + 3 个切点 → B-roll 素材被循环使用 3 次"""
    from capcut_draft_core import builder

    script = mock_pyjyd["script"]
    kwargs = _builder_kw(
        tmp_path,
        broll_clips=[str(tmp_path / "broll1.mp4")],
        cut_points=[CutPoint(time=2.0), CutPoint(time=6.0), CutPoint(time=10.0)],
    )
    with patch.object(builder, "_probe_video_duration", return_value=30.0), \
         patch.object(builder.shutil, "copy2"), \
         patch.object(builder.shutil, "rmtree"):
        builder.build_draft(**kwargs)

    assert script.add_material.call_count == 4
    broll_segs = [
        c for c in script.add_segment.call_args_list
        if c.kwargs.get("track_name") == "broll_overlay"
    ]
    assert len(broll_segs) == 3


# ---- 场景 7: B-roll 太短 ----


def test_builder_broll_too_short(tmp_path, mock_pyjyd):
    """mock _probe_video_duration 对 B-roll 返回 0.2 → 跳过"""
    from capcut_draft_core import builder

    script = mock_pyjyd["script"]
    kwargs = _builder_kw(tmp_path)
    with patch.object(builder, "_probe_video_duration",
                      side_effect=[30.0, 0.2]), \
         patch.object(builder.shutil, "copy2"), \
         patch.object(builder.shutil, "rmtree"):
        builder.build_draft(**kwargs)

    broll_segs = [
        c for c in script.add_segment.call_args_list
        if c.kwargs.get("track_name") == "broll_overlay"
    ]
    assert len(broll_segs) == 0


# ---- 场景 8: 切点越界 ----


def test_builder_cut_point_out_of_bounds(tmp_path, mock_pyjyd):
    """切点时间 > 主视频时长 → B-roll 被跳过"""
    from capcut_draft_core import builder

    script = mock_pyjyd["script"]
    kwargs = _builder_kw(tmp_path, cut_points=[CutPoint(time=31.0)])
    with patch.object(builder, "_probe_video_duration", return_value=30.0), \
         patch.object(builder.shutil, "copy2"), \
         patch.object(builder.shutil, "rmtree"):
        builder.build_draft(**kwargs)

    broll_segs = [
        c for c in script.add_segment.call_args_list
        if c.kwargs.get("track_name") == "broll_overlay"
    ]
    assert len(broll_segs) == 0


# ---- 场景 9: 视频时长探测失败 ----


def test_builder_probe_failure(tmp_path, mock_pyjyd):
    """_probe_video_duration 抛出 RuntimeError → 异常传播"""
    from capcut_draft_core import builder

    kw = _builder_kw(tmp_path)
    with patch.object(builder, "_probe_video_duration",
                      side_effect=RuntimeError("无法解析视频时长")), \
         patch.object(builder.shutil, "copy2"), \
         patch.object(builder.shutil, "rmtree"):
        with pytest.raises(RuntimeError, match="无法解析视频时长"):
            builder.build_draft(**kw)


# ---- 场景 10: 空文本段 ----


def test_builder_empty_text_segment(tmp_path, mock_pyjyd):
    """Segment(text="", ...) → 被跳过"""
    from capcut_draft_core import builder

    script = mock_pyjyd["script"]
    kwargs = _builder_kw(
        tmp_path,
        segments=[
            Segment(text="", start=0.0, end=1.0),
            Segment(text="有效", start=1.0, end=2.5),
        ],
        cut_points=[],
    )
    with patch.object(builder, "_probe_video_duration", return_value=30.0), \
         patch.object(builder.shutil, "copy2"), \
         patch.object(builder.shutil, "rmtree"):
        builder.build_draft(**kwargs)

    text_segs = [
        c for c in script.add_segment.call_args_list
        if c.kwargs.get("track_name") == "subtitle"
    ]
    assert len(text_segs) == 1
