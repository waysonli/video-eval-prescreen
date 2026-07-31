#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频评测自动化初筛 · 批量执行脚本

流程: 抽帧(ffmpeg) -> 上传Dify -> 调用工作流 -> 汇总结果

用法:
    export DIFY_API_KEY="app-xxxxxxxx"
    export DIFY_BASE_URL="https://api.dify.ai/v1"     # 私有部署改成自己的地址
    python3 run_prescreen.py --videos ./videos --out ./results --scene 通用视频

依赖:
    pip3 install requests
    brew install ffmpeg
"""

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("缺少依赖，请先执行: pip3 install requests")


# ---------------------------------------------------------------- 配置

API_KEY = os.environ.get("DIFY_API_KEY", "")
BASE_URL = os.environ.get("DIFY_BASE_URL", "https://api.dify.ai/v1").rstrip("/")
USER_ID = os.environ.get("DIFY_USER_ID", "prescreen-batch")

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
MAX_FRAMES = 8          # 与工作流 start 节点 max_length 保持一致
SCENE_THRESHOLD = 0.3   # 场景切换检测灵敏度，越小越敏感
INTERVAL_SEC = 1.0      # 固定间隔抽帧的间隔秒数
REQUEST_TIMEOUT = 180


# ---------------------------------------------------------------- 抽帧

def probe_duration(video: Path) -> float:
    """用 ffprobe 取视频时长，失败返回 0"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def extract_frames(video: Path, outdir: Path) -> tuple:
    """
    分层抽帧: 场景切换帧 + 固定间隔帧 + 首尾帧
    返回 (帧文件路径列表, 元信息dict)
    """
    outdir.mkdir(parents=True, exist_ok=True)
    for old in outdir.glob("*.jpg"):
        old.unlink()

    duration = probe_duration(video)

    # 第一轮: 场景切换帧
    scene_dir = outdir / "scene"
    scene_dir.mkdir(exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
            "-vf", "select='gt(scene,%s)',scale=768:-2" % SCENE_THRESHOLD,
            "-vsync", "vfr", "-q:v", "3",
            str(scene_dir / "cut_%03d.jpg"),
        ],
        capture_output=True, timeout=300,
    )
    scene_frames = sorted(scene_dir.glob("cut_*.jpg"))

    # 第二轮: 固定间隔帧
    interval_dir = outdir / "interval"
    interval_dir.mkdir(exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
            "-vf", "fps=%s,scale=768:-2" % (1.0 / INTERVAL_SEC),
            "-q:v", "3",
            str(interval_dir / "iv_%03d.jpg"),
        ],
        capture_output=True, timeout=300,
    )
    interval_frames = sorted(interval_dir.glob("iv_*.jpg"))

    # 组装: 首帧 + 场景切换帧优先 + 间隔帧补足 + 尾帧
    selected = []
    if interval_frames:
        selected.append(interval_frames[0])

    for f in scene_frames:
        if len(selected) >= MAX_FRAMES - 1:
            break
        selected.append(f)

    if interval_frames:
        middle = interval_frames[1:-1]
        if middle:
            need = MAX_FRAMES - 1 - len(selected)
            if need > 0:
                step = max(1, len(middle) // need)
                for f in middle[::step][:need]:
                    selected.append(f)
        if len(interval_frames) > 1 and len(selected) < MAX_FRAMES:
            selected.append(interval_frames[-1])

    # 落盘为统一命名，便于人工回看
    final = []
    for idx, src in enumerate(selected[:MAX_FRAMES], start=1):
        dst = outdir / ("f%02d.jpg" % idx)
        shutil.copy(src, dst)
        final.append(dst)

    shutil.rmtree(scene_dir, ignore_errors=True)
    shutil.rmtree(interval_dir, ignore_errors=True)

    meta = {
        "duration_sec": round(duration, 2),
        "scene_cut_detected": len(scene_frames),
        "frames_used": len(final),
        "scene_threshold": SCENE_THRESHOLD,
    }
    return final, meta


# ---------------------------------------------------------------- Dify 调用

def upload_file(path: Path) -> str:
    """上传单张图片，返回 upload_file_id"""
    with open(path, "rb") as fh:
        resp = requests.post(
            BASE_URL + "/files/upload",
            headers={"Authorization": "Bearer " + API_KEY},
            files={"file": (path.name, fh, "image/jpeg")},
            data={"user": USER_ID},
            timeout=60,
        )
    resp.raise_for_status()
    return resp.json()["id"]


def run_workflow(file_ids: list, sample_id: str, prompt_text: str,
                 scene_type: str, frame_meta: dict) -> dict:
    """调用工作流，blocking 模式直接拿结果"""
    payload = {
        "inputs": {
            "frames": [
                {
                    "transfer_method": "local_file",
                    "upload_file_id": fid,
                    "type": "image",
                }
                for fid in file_ids
            ],
            "sample_id": sample_id,
            "prompt_text": prompt_text,
            "scene_type": scene_type,
            "frame_meta": json.dumps(frame_meta, ensure_ascii=False),
        },
        "response_mode": "blocking",
        "user": USER_ID,
    }
    resp = requests.post(
        BASE_URL + "/workflows/run",
        headers={
            "Authorization": "Bearer " + API_KEY,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------- 单条处理

def process_one(video: Path, workdir: Path, scene_type: str,
                prompt_map: dict) -> dict:
    sample_id = video.stem
    record = {"sample_id": sample_id, "video": str(video)}

    try:
        frames, meta = extract_frames(video, workdir / sample_id)
        if not frames:
            record.update(status="failed", error="抽帧结果为空")
            return record

        file_ids = [upload_file(f) for f in frames]
        prompt_text = prompt_map.get(sample_id, "")

        result = run_workflow(file_ids, sample_id, prompt_text, scene_type, meta)
        data = result.get("data", {})

        if data.get("status") != "succeeded":
            record.update(
                status="failed",
                error=str(data.get("error", "工作流未成功"))[:200],
            )
            return record

        outputs = data.get("outputs", {}) or {}
        detail = {}
        try:
            detail = json.loads(outputs.get("result_json", "{}"))
        except Exception:
            pass

        record.update(
            status="succeeded",
            route=outputs.get("route", ""),
            confidence=outputs.get("confidence", ""),
            review_dims=outputs.get("review_dims", ""),
            reason=detail.get("reason", ""),
            redline_hit=detail.get("redline_hit", ""),
            rule_flags=detail.get("rule_flags", ""),
            scores=json.dumps(detail.get("scores", {}), ensure_ascii=False),
            frames_used=meta["frames_used"],
            elapsed=data.get("elapsed_time", 0),
            raw=json.dumps(detail, ensure_ascii=False),
        )
    except requests.HTTPError as exc:
        body = exc.response.text[:200] if exc.response is not None else ""
        record.update(status="failed", error="HTTP %s %s" % (
            exc.response.status_code if exc.response is not None else "?", body))
    except Exception as exc:
        record.update(status="failed", error=str(exc)[:200])

    return record


# ---------------------------------------------------------------- 主流程

def load_prompts(path: Path) -> dict:
    """可选: 读取 sample_id -> prompt 的映射 CSV(两列: sample_id,prompt)"""
    if not path or not path.exists():
        return {}
    mapping = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            mapping[row["sample_id"].strip()] = row.get("prompt", "").strip()
    return mapping


def main():
    parser = argparse.ArgumentParser(description="视频评测自动化初筛批量执行")
    parser.add_argument("--videos", required=True, help="视频目录")
    parser.add_argument("--out", default="./results", help="输出目录")
    parser.add_argument("--scene", default="通用视频",
                        choices=["通用视频", "电商R2V"], help="场景类型")
    parser.add_argument("--prompts", default="", help="prompt映射CSV，可选")
    parser.add_argument("--workers", type=int, default=3, help="并发数")
    parser.add_argument("--limit", type=int, default=0, help="只跑前N条，0为不限")
    args = parser.parse_args()

    if not API_KEY:
        sys.exit("请先设置环境变量 DIFY_API_KEY")
    if not shutil.which("ffmpeg"):
        sys.exit("未找到 ffmpeg，请执行: brew install ffmpeg")

    video_dir = Path(args.videos)
    out_dir = Path(args.out)
    frames_dir = out_dir / "frames"
    out_dir.mkdir(parents=True, exist_ok=True)

    videos = sorted(
        p for p in video_dir.iterdir()
        if p.suffix.lower() in VIDEO_EXTS
    )
    if args.limit:
        videos = videos[: args.limit]
    if not videos:
        sys.exit("目录中没有找到视频文件: %s" % video_dir)

    # 断点续跑: 已成功的样本跳过
    jsonl_path = out_dir / "results.jsonl"
    done = set()
    if jsonl_path.exists():
        with open(jsonl_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                    if rec.get("status") == "succeeded":
                        done.add(rec["sample_id"])
                except Exception:
                    continue
    pending = [v for v in videos if v.stem not in done]

    print("共 %d 条，已完成 %d 条，本次待处理 %d 条"
          % (len(videos), len(done), len(pending)))
    if not pending:
        return

    prompt_map = load_prompts(Path(args.prompts)) if args.prompts else {}
    results = []
    start_ts = time.time()

    with open(jsonl_path, "a", encoding="utf-8") as sink:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(process_one, v, frames_dir, args.scene, prompt_map): v
                for v in pending
            }
            for idx, fut in enumerate(as_completed(futures), start=1):
                rec = fut.result()
                results.append(rec)
                sink.write(json.dumps(rec, ensure_ascii=False) + "\n")
                sink.flush()
                flag = "ok " if rec["status"] == "succeeded" else "ERR"
                print("[%d/%d] %s %-28s %s %s" % (
                    idx, len(pending), flag, rec["sample_id"],
                    rec.get("route", ""), rec.get("error", "")))

    # 汇总 CSV
    csv_path = out_dir / "summary.csv"
    cols = ["sample_id", "status", "route", "confidence", "review_dims",
            "reason", "redline_hit", "rule_flags", "scores",
            "frames_used", "elapsed", "error"]
    write_header = not csv_path.exists()
    with open(csv_path, "a", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        for rec in results:
            writer.writerow(rec)

    ok = [r for r in results if r["status"] == "succeeded"]
    routes = {}
    for r in ok:
        routes[r.get("route", "")] = routes.get(r.get("route", ""), 0) + 1

    print("\n耗时 %.1f 秒，成功 %d / %d" % (time.time() - start_ts, len(ok), len(results)))
    print("分流分布:")
    for name, cnt in sorted(routes.items(), key=lambda x: -x[1]):
        pct = cnt / len(ok) * 100 if ok else 0
        print("  %-16s %4d 条  %.1f%%" % (name, cnt, pct))
    if ok:
        auto = sum(c for k, c in routes.items() if k != "edge_review")
        print("\n可自动处理占比 %.1f%%，需人工复核 %.1f%%"
              % (auto / len(ok) * 100, (len(ok) - auto) / len(ok) * 100))
    print("\n明细: %s\n汇总: %s\n帧图: %s" % (jsonl_path, csv_path, frames_dir))


if __name__ == "__main__":
    main()
