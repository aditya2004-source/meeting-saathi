#!/usr/bin/env python3
"""Before/after timing comparison: the legacy whole-file pipeline
(diarize()) vs. the new chunked pipeline (diarize_chunk(), worst case --
every chunk forced through the pyannote fallback by passing an empty
speaker_events list, since a real recording's DOM coverage will only do
better than this).

This never touches the live server, port 8420, or the database -- it calls
app.pipeline.diarize functions directly on a local audio file.

Usage:
    # Against a real recording already on disk:
    python scripts/benchmark_pipeline.py /path/to/recording.webm [--chunk-seconds 50]

    # No long recording on hand? Synthesize one by looping/concatenating
    # existing short clips to a target duration (fine for *timing* purposes
    # -- cost scales with audio duration/characteristics, not semantic
    # content):
    python scripts/benchmark_pipeline.py --synthesize-from working/ --target-minutes 65 [--chunk-seconds 50]
"""
import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import av  # noqa: E402

from app.pipeline.diarize import _align, _pyannote_turns, diarize, diarize_chunk, probe_duration_seconds  # noqa: E402
from app.pipeline.transcribe import transcribe  # noqa: E402

_SLICE_RATE = 48000


def _decode_mono_pcm(path: Path, resample_rate: int = _SLICE_RATE):
    """Decodes a webm/opus file to a flat list of resampled mono frames,
    ready to be re-encoded into new standalone files."""
    container = av.open(str(path))
    stream = container.streams.audio[0]
    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=resample_rate)
    frames = []
    for frame in container.decode(stream):
        frames.extend(resampler.resample(frame))
    container.close()
    return frames


def _write_webm(frames, dest_path: Path) -> None:
    out = av.open(str(dest_path), mode="w", format="webm")
    out_stream = out.add_stream("libopus", rate=_SLICE_RATE)
    out_stream.codec_context.layout = "mono"
    for frame in frames:
        # Frames concatenated from multiple source decodes (or looped from
        # one) carry their original, non-monotonic source pts -- clearing
        # it lets the encoder assign fresh, strictly monotonic timestamps
        # based on encode order instead, which the webm muxer requires.
        frame.pts = None
        for packet in out_stream.encode(frame):
            out.mux(packet)
    for packet in out_stream.encode(None):
        out.mux(packet)
    out.close()


def synthesize_long_recording(source_dir: Path, target_minutes: float, dest_path: Path) -> Path:
    """Loops/concatenates every *.webm found under source_dir until the
    combined audio reaches target_minutes, writing one combined file. Used
    when no long real recording is available to benchmark with."""
    clips = sorted(source_dir.rglob("*.webm"))
    if not clips:
        raise SystemExit(f"No .webm files found under {source_dir}")

    print(f"Synthesizing ~{target_minutes:.1f} min from {len(clips)} source clip(s): "
          f"{', '.join(c.name for c in clips)}")

    per_clip_frames = [_decode_mono_pcm(c) for c in clips]
    per_clip_frames = [frames for frames in per_clip_frames if frames]
    if not per_clip_frames:
        raise SystemExit("Source clips decoded to zero audio frames -- nothing to synthesize from.")

    target_seconds = target_minutes * 60
    accumulated = []
    accumulated_seconds = 0.0
    i = 0
    while accumulated_seconds < target_seconds:
        frames = per_clip_frames[i % len(per_clip_frames)]
        accumulated.extend(frames)
        accumulated_seconds += sum(f.samples / f.sample_rate for f in frames)
        i += 1

    _write_webm(accumulated, dest_path)
    actual_duration = probe_duration_seconds(dest_path)
    print(f"Wrote synthesized recording: {dest_path} ({actual_duration / 60:.1f} min)")
    return dest_path


def slice_into_chunks(source_path: Path, chunk_seconds: float, dest_dir: Path) -> list[Path]:
    """Re-encodes source_path into standalone, independently-decodable
    ~chunk_seconds webm/opus files -- a plain byte-slice of one continuous
    recording isn't decodable (only the first slice would carry the
    container's init segment), so each chunk needs its own encode pass,
    same as what a real MediaRecorder restart-cycle produces."""
    frames = _decode_mono_pcm(source_path)
    dest_dir.mkdir(parents=True, exist_ok=True)

    chunks = []
    current_frames = []
    current_seconds = 0.0
    sequence = 0
    for frame in frames:
        current_frames.append(frame)
        current_seconds += frame.samples / frame.sample_rate
        if current_seconds >= chunk_seconds:
            dest = dest_dir / f"{sequence}.webm"
            _write_webm(current_frames, dest)
            chunks.append(dest)
            sequence += 1
            current_frames = []
            current_seconds = 0.0
    if current_frames:
        dest = dest_dir / f"{sequence}.webm"
        _write_webm(current_frames, dest)
        chunks.append(dest)
    return chunks


def benchmark_whole_file(path: Path) -> dict:
    print("\n=== (a) Whole-file / legacy path ===")
    t0 = time.monotonic()
    transcribed = transcribe(path)
    t1 = time.monotonic()
    turns = _pyannote_turns(path)
    t2 = time.monotonic()
    segments = _align(transcribed, turns)
    t3 = time.monotonic()
    print(f"transcribe:       {t1 - t0:8.1f}s")
    print(f"pyannote:         {t2 - t1:8.1f}s")
    print(f"align:            {t3 - t2:8.2f}s")
    print(f"TOTAL:            {t3 - t0:8.1f}s   ({len(segments)} segments)")
    return {"transcribe": t1 - t0, "pyannote": t2 - t1, "align": t3 - t2, "total": t3 - t0}


def benchmark_chunked(chunks: list[Path]) -> dict:
    print(f"\n=== (b) Chunked path, worst case ({len(chunks)} chunks, "
          f"empty speaker_events forces pyannote fallback on every chunk) ===")
    offset = 0.0
    total_transcribe = 0.0
    total_pyannote = 0.0
    t_start = time.monotonic()
    all_segments = []
    for i, chunk_path in enumerate(chunks):
        duration = probe_duration_seconds(chunk_path)
        t0 = time.monotonic()
        segments = diarize_chunk(
            chunk_path,
            speaker_events=[],
            chunk_start_offset=offset,
            chunk_end_offset=offset + duration,
        )
        t1 = time.monotonic()
        all_segments.extend(segments)
        print(f"  chunk {i:>3} ({duration:5.1f}s audio): {t1 - t0:6.1f}s")
        offset += duration
    total = time.monotonic() - t_start
    print(f"TOTAL:            {total:8.1f}s   ({len(all_segments)} segments)")
    return {"total": total, "chunk_count": len(chunks)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("recording", nargs="?", type=Path, help="Path to an existing recording (webm)")
    parser.add_argument("--synthesize-from", type=Path, help="Directory of .webm clips to synthesize a long test recording from")
    parser.add_argument("--target-minutes", type=float, default=65.0, help="Target duration when synthesizing (default: 65)")
    parser.add_argument("--chunk-seconds", type=float, default=50.0, help="Chunk length for the chunked-path benchmark (default: 50)")
    parser.add_argument("--skip-whole-file", action="store_true", help="Skip the slow legacy whole-file benchmark")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="meeting_saathi_benchmark_") as tmp:
        tmp_dir = Path(tmp)

        if args.recording:
            recording_path = args.recording
        elif args.synthesize_from:
            recording_path = synthesize_long_recording(
                args.synthesize_from, args.target_minutes, tmp_dir / "synthesized.webm"
            )
        else:
            parser.error("Provide a recording path, or --synthesize-from <dir> --target-minutes N")
            return

        duration = probe_duration_seconds(recording_path)
        print(f"Input: {recording_path} ({duration / 60:.1f} min)")

        results = {}
        if not args.skip_whole_file:
            results["whole_file"] = benchmark_whole_file(recording_path)
        else:
            print("\n=== (a) Whole-file / legacy path: SKIPPED (--skip-whole-file) ===")

        chunks = slice_into_chunks(recording_path, args.chunk_seconds, tmp_dir / "chunks")
        results["chunked"] = benchmark_chunked(chunks)

        print("\n=== Summary ===")
        if "whole_file" in results:
            print(f"Whole-file (legacy) TOTAL: {results['whole_file']['total']:.1f}s")
        print(f"Chunked (worst case) TOTAL: {results['chunked']['total']:.1f}s "
              f"across {results['chunked']['chunk_count']} chunks")
        print("(Note: 'extract facts + 3x generate' and '3x PDF render' stages are identical on both "
              "paths -- see app/docgen/engine.py and app/docgen/render_pdf.py, already benchmarked "
              "separately since they don't depend on audio at all.)")


if __name__ == "__main__":
    main()
