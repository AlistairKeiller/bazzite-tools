#!/usr/bin/env python3
"""One-command room correction: measure the speaker, build the filter, apply it.

Usage:
  ./calibrate.py                   # measure, correct, then measure the result
  ./calibrate.py --reuse --strength strong   # new filter from the last sweep
  ./calibrate.py --flat            # remove the correction

Plays a 20 s sweep on the speaker, records it with the measurement mic, and
hands the recording to DRC-FIR (in the room-eq-drc container that install.sh
builds), which computes a 65536-tap correction filter towards target.txt. The
filter goes into the CamillaDSP config and is live at once; a second sweep
through the corrected chain then shows what was actually achieved.

Before running: mic plugged into the interface's XLR input with 48V on, INST
and AIR off, Direct Monitor OFF, mic at ear height at the listening position
pointing at the speaker, room quiet and nothing else playing. Every input of
the interface is recorded and the one that hears the sweep is used. Start with the speaker volume low and raise it
if the script says the signal is too quiet.

--strength picks one of DRC's stock profiles: stronger corrects more of the
room's reverberant field at that exact mic position, at the cost of sounding
worse away from it. The target curve is target.txt next to this script
("Hz dB" points, 0 Hz to 24000 Hz). --cal is the mic's calibration file and
defaults to the one for my EMM-6 next to this script.

The config always mixes stereo down to mono and plays it on the first (left)
output only, because there is a single speaker.
"""
import argparse
import array
import cmath
import json
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path.home() / "camilladsp"
CONFIG = HOME / "configs" / "room-eq.yml"
COEFFS = HOME / "coeffs"
WORK = HOME / "measurement"
OUTPUT_FILE = HOME / "output-node"  # written by install.sh: the node the speaker is on
EQ_SINK = "camilladsp-sink"
CONTAINER = ["podman", "run", "--rm", "--security-opt", "label=disable"]
IMAGE = "room-eq-drc"
RATE = 48000
SWEEP_SECONDS = 20
SWEEP_AMPLITUDE = 0.5
# The range the before/after flatness figures cover: where target.txt is flat.
FLAT_BAND = (70.0, 16000.0)


def die(msg):
    sys.exit(f"error: {msg}")


def drc_tool(*cmd):
    """Run one of DRC's programs in the container, with WORK mounted as /work."""
    run = subprocess.run([*CONTAINER, "-v", f"{WORK}:/work", "-w", "/work", IMAGE, *cmd],
                         capture_output=True, text=True)
    if run.returncode != 0:
        die(f"{cmd[0]} failed (has install.sh built the {IMAGE} image?):\n"
            f"{run.stdout[-3000:]}{run.stderr[-3000:]}")


def read_floats(path):
    samples = array.array("f")
    samples.frombytes(Path(path).read_bytes())
    return samples


# --- Playing and recording through PipeWire ----------------------------------

def devices(media_class, name=None):
    """[(node.name, [channel names])] of node `name`, or of every Scarlett node."""
    dump = json.loads(subprocess.run(["pw-dump"], check=True, capture_output=True, text=True).stdout)

    def props(obj):
        return (obj.get("info") or {}).get("props") or {}

    found = []
    for node in dump:
        if node.get("type") != "PipeWire:Interface:Node" or props(node).get("media.class") != media_class:
            continue
        label = f"{props(node).get('node.name')} {props(node).get('node.description')}"
        if not (props(node).get("node.name") == name if name else "scarlett" in label.lower()):
            continue
        direction = "input" if media_class == "Audio/Sink" else "output"
        ports = sorted((int(props(o).get("port.id", 0)), props(o)["audio.channel"]) for o in dump
                       if o.get("type") == "PipeWire:Interface:Port"
                       and props(o).get("node.id") == node["id"]
                       and o["info"].get("direction") == direction and "audio.channel" in props(o))
        if ports:
            found.append((props(node)["node.name"], [channel for _, channel in ports]))
    if not found:
        die(f"no {media_class} named {name or '*Scarlett*'}; is the interface plugged in?")
    return found


def write_wav(path, mono, channels, active):
    """Write `mono` as a float WAV with `channels` channels, silent except those in `active`."""
    frames = array.array("f", bytes(4 * channels * len(mono)))
    for ch in active:
        frames[ch::channels] = mono
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + 4 * len(frames)) + b"WAVEfmt "
                + struct.pack("<IHHIIHH", 16, 3, channels, RATE, RATE * channels * 4, channels * 4, 32)
                + b"data" + struct.pack("<I", 4 * len(frames)))
        frames.tofile(f)


def read_wav_channel(path, index):
    """One channel of a 32-bit float WAV.

    The stdlib wave module rejects float files, and a recorder stopped by a
    signal may leave the size fields unset, so walk the chunks by hand.
    """
    data = Path(path).read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        die(f"{path} is not a WAV file")
    pos, fmt = 12, None
    while pos + 8 <= len(data):
        chunk, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = pos + 8
        if chunk == b"fmt ":
            tag, channels, _, _, _, bits = struct.unpack("<HHIIHH", data[body:body + 16])
            if tag == 0xFFFE:  # WAVE_FORMAT_EXTENSIBLE: the real tag starts the GUID
                tag = struct.unpack("<H", data[body + 24:body + 26])[0]
            fmt = (tag, channels, bits)
        elif chunk == b"data" and fmt:
            tag, channels, bits = fmt
            if (tag, bits) != (3, 32) or index >= channels:
                die(f"unexpected recording format in {path} (tag {tag}, {bits} bit, {channels} channels)")
            raw = data[body:body + size] if 0 < size <= len(data) - body else data[body:]
            samples = array.array("f")
            samples.frombytes(raw[:len(raw) // (4 * channels) * (4 * channels)])
            return samples[index::channels]
        pos = body + size + (size & 1)
    die(f"no audio data in {path}")


def record(source, channels, path):
    return subprocess.Popen(
        ["pw-record", "--target", source, "--rate", str(RATE), "--format", "f32",
         "--channels", str(len(channels)), "--channel-map", ",".join(channels), str(path)])


def sweep_and_record(name, sink, active=None, mic=None):
    """Play sweep.pcm on `sink` (on the `active` channels, default all) and
    leave what the mic heard in WORK/<name>.pcm.

    `mic` is (source node, channel index). Without it every input of the
    interface is recorded and the loudest one is used. Returns the mic used.
    """
    (_, sink_channels), = devices("Audio/Sink", sink)
    sources = devices("Audio/Source", mic[0]) if mic else devices("Audio/Source")
    play_path = WORK / "play.wav"
    write_wav(play_path, read_floats(WORK / "sweep.pcm"), len(sink_channels),
              range(len(sink_channels)) if active is None else active)
    for n in (3, 2, 1):
        print(f"\r    sweep starts in {n}... (about {SWEEP_SECONDS + 6} s, keep quiet)", end="", flush=True)
        time.sleep(1)
    print()
    recorders = [(source, WORK / f"rec{i}.wav", record(source, channels, WORK / f"rec{i}.wav"))
                 for i, (source, channels) in enumerate(sources)]
    try:
        time.sleep(1.0)
        if any(rec.poll() is not None for _, _, rec in recorders):
            die("pw-record could not start recording")
        subprocess.run(["pw-play", "--target", sink, "--channel-map", ",".join(sink_channels),
                        str(play_path)], check=True)
        time.sleep(1.0)
    finally:
        for _, _, rec in recorders:
            rec.send_signal(signal.SIGINT)
        for _, _, rec in recorders:
            try:
                rec.wait(timeout=10)
            except subprocess.TimeoutExpired:
                rec.kill()
    takes = []  # (energy, source, channel index, samples)
    for (source, channels), (_, path, _) in zip(sources, recorders):
        for index in ([mic[1]] if mic else range(len(channels))):
            samples = read_wav_channel(path, index)
            takes.append((sum(v * v for v in samples), source, index, samples))
        path.unlink()
    play_path.unlink()
    _, source, index, mono = max(takes, key=lambda take: take[0])
    peak = max(max(mono, default=0.0), -min(mono, default=0.0))
    print(f"    {sink} -> {source} input {index + 1}, peak {20 * math.log10(max(peak, 1e-10)):.1f} dBFS")
    if peak > 0.98:
        die("the input is clipping: turn the input gain or the speaker down and run again")
    with open(WORK / f"{name}.pcm", "wb") as f:
        mono.tofile(f)
    return source, index


def impulse_response(recording):
    """Deconvolve WORK/<recording>.pcm and return (impulse response, index of its peak)."""
    drc_tool("lsconv", f"{recording}.pcm", "inverse.pcm", f"{recording}-impulse.pcm")
    ir = read_floats(WORK / f"{recording}-impulse.pcm")
    peak = max(range(len(ir)), key=lambda i: abs(ir[i]))
    # Nothing arrives before the direct sound, so that stretch is the noise floor.
    noise = ir[max(peak - 7 * RATE // 10, 0):max(peak - 2 * RATE // 10, 0)]
    rms = math.sqrt(sum(s * s for s in noise) / len(noise)) if noise else 0.0
    snr = 20 * math.log10(abs(ir[peak]) / rms) if rms > 0 and ir[peak] else 0.0
    print(f"    signal to noise {snr:.0f} dB")
    if snr < 40:
        die("the mic did not pick the sweep up clearly: check 48V, the input gain, "
            "the speaker volume and that the room is quiet, then run again")
    return ir, peak


# --- Analysis (only for the report and the headroom; DRC does the real work) --

def spectrum_db(samples, size=1 << 16):
    """Power spectrum in dB of `samples` zero-padded to `size`, bins 0..size/2."""
    bits = size.bit_length() - 1
    x = [0j] * size
    for i, s in enumerate(samples[:size]):
        x[int(f"{i:0{bits}b}"[::-1], 2)] = complex(s)
    half = 1
    while half < size:
        step = cmath.exp(-1j * math.pi / half)
        for start in range(0, size, 2 * half):
            w = 1 + 0j
            for k in range(start, start + half):
                a, b = x[k], x[k + half] * w
                x[k], x[k + half] = a + b, a - b
                w *= step
        half *= 2
    return [10 * math.log10(abs(v) ** 2 + 1e-30) for v in x[:size // 2 + 1]]


def load_cal(path):
    """[(Hz, dB)] from a Dayton-style 'freq dB [phase]' calibration file."""
    rows = {}
    for line in Path(path).read_text(errors="replace").splitlines():
        try:
            f, db = (float(v) for v in line.replace(",", " ").split()[:2])
        except ValueError:
            continue  # header lines such as '*1000Hz -39.1'
        if 0 < f < RATE / 2:
            rows[f] = db
    if len(rows) < 10:
        die(f"{path} does not look like a mic calibration file")
    return sorted(rows.items())


def interpolate(points, freq):
    if freq <= points[0][0]:
        return points[0][1]
    for (f0, d0), (f1, d1) in zip(points, points[1:]):
        if freq <= f1:
            return d0 + (d1 - d0) * math.log(freq / f0) / math.log(f1 / f0)
    return points[-1][1]


def response(ir, peak, cal):
    """{Hz: dB} in sixth-octave bands, from 10 ms before to 500 ms after the direct sound."""
    pre, post = RATE // 100, RATE // 2
    seg = list(ir[max(peak - pre, 0):peak + post])
    for i in range(min(pre, len(seg))):
        seg[i] *= math.sin(math.pi / 2 * i / pre) ** 2
    for i in range(min(post // 2, len(seg))):
        seg[-1 - i] *= math.sin(math.pi / 2 * i / (post // 2)) ** 2
    size = 1 << 16
    power = [10 ** (db / 10) for db in spectrum_db(seg, size)]
    bands = {}
    for n in range(61):
        centre = 20 * 2 ** (n / 6)
        lo = int(centre * 2 ** (-1 / 12) * size / RATE)
        hi = max(int(centre * 2 ** (1 / 12) * size / RATE), lo + 1)
        bands[centre] = 10 * math.log10(sum(power[lo:hi]) / (hi - lo)) - interpolate(cal, centre)
    return bands


def flatness(bands):
    """(mean level, largest deviation from it) over FLAT_BAND."""
    inside = [db for f, db in bands.items() if FLAT_BAND[0] <= f <= FLAT_BAND[1]]
    mean = sum(inside) / len(inside)
    return mean, max(abs(db - mean) for db in inside)


def report(before, after):
    print("\n     Hz    before" + ("     after" if after else ""))
    for f in list(before)[::2]:  # third-octave rows
        row = f"{f:7.0f}  {before[f] - flatness(before)[0]:+6.1f} dB"
        print(row + (f"  {after[f] - flatness(after)[0]:+6.1f} dB" if after else ""))
    line = f"{FLAT_BAND[0]:.0f} Hz - {FLAT_BAND[1] / 1000:.0f} kHz: within +/-{flatness(before)[1]:.1f} dB before"
    print(line + (f", +/-{flatness(after)[1]:.1f} dB after" if after else "") + "\n")


# --- CamillaDSP config -------------------------------------------------------

def current_output():
    """The node the speaker is on, as chosen by install.sh."""
    if not OUTPUT_FILE.exists():
        die(f"{OUTPUT_FILE} not found; run install.sh first")
    return OUTPUT_FILE.read_text().strip()


def install_config(note, filter_path=None, headroom=0.0):
    """Write and activate the config; returns whether CamillaDSP is running with it."""
    correction, names = "", "[]"
    if filter_path:
        names = "[headroom, room_correction]"
        correction = f"""
filters:
  # The correction boosts some frequencies; take that off first so it cannot clip.
  headroom:
    type: Gain
    parameters:
      gain: {-headroom:.2f}
  room_correction:
    type: Conv
    parameters:
      type: Raw
      filename: "{filter_path}"
      format: F32_LE
"""
    text = f"""# Generated by calibrate.py ({note}). Edits made here or in CamillaGUI
# (http://localhost:5005) are overwritten the next time that script runs.
# The two devices are wired up by camilladsp-link.service, not by this file.
description: "Room EQ for the KH 120A ({note})"
devices:
  samplerate: {RATE}
  chunksize: 1024
  capture:
    type: PipeWire
    channels: 2
  playback:
    type: PipeWire
    channels: 2

mixers:
  # One speaker on the left output: play L+R there and leave the right silent.
  mono_to_left:
    channels:
      in: 2
      out: 2
    mapping:
      - dest: 0
        sources:
          - channel: 0
            gain: -6
          - channel: 1
            gain: -6
{correction}
pipeline:
  - type: Mixer
    name: mono_to_left
  - type: Filter
    channels: [0]
    names: {names}
"""
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".yml.new")
    tmp.write_text(text)
    check = subprocess.run([str(HOME / "camilladsp"), "--check", str(tmp)], capture_output=True, text=True)
    if check.returncode != 0:
        tmp.unlink()
        die(f"camilladsp rejected the config, keeping the old one:\n{check.stdout}{check.stderr}")
    os.replace(tmp, CONFIG)
    for old in COEFFS.glob("room-eq-*.pcm"):
        if old != filter_path:
            old.unlink()
    print(f"==> Wrote {CONFIG} ({note})")
    if subprocess.run(["systemctl", "--user", "is-active", "--quiet", "camilladsp.service"]).returncode != 0:
        print("warning: camilladsp.service is not running; run install.sh", file=sys.stderr)
        return False
    subprocess.run(["systemctl", "--user", "reload", "camilladsp.service"], check=True)
    return True


# --- Main --------------------------------------------------------------------

def calibrate(args):
    cal = load_cal(args.cal)
    output = current_output()
    WORK.mkdir(parents=True, exist_ok=True)
    COEFFS.mkdir(parents=True, exist_ok=True)

    if args.reuse:
        if not (WORK / "recording.pcm").exists():
            die("nothing to reuse yet; run without --reuse first")
    else:
        print("==> Measuring the speaker")
        drc_tool("glsweep", str(RATE), str(SWEEP_AMPLITUDE), "10", "21000", str(SWEEP_SECONDS),
                 "2", "0.05", "0.005", "sweep.pcm", "inverse.pcm")
        # Straight to the interface's first output, bypassing the current correction.
        mic = sweep_and_record("recording", output, active=[0])
    before = response(*impulse_response("recording"), cal)

    print(f"==> Computing the correction (DRC, {args.strength})")
    shutil.copyfile(args.target, WORK / "target.txt")
    # DRC wants the mic's response from 0 Hz to Nyquist.
    points = [(0.0, cal[0][1]), *cal, (RATE / 2, cal[-1][1])]
    (WORK / "mic.txt").write_text("".join(f"{f:.4f} {db:.4f}\n" for f, db in points))
    drc_tool("drc", "--BCInFile=recording-impulse.pcm", "--PSPointsFile=target.txt",
             "--PSOutFile=filter.pcm", "--MCFilterType=M", "--MCPointsFile=mic.txt",
             f"/usr/share/drc/config/48.0 kHz/{args.strength}-48.0.drc")
    taps = read_floats(WORK / "filter.pcm")
    if len(taps) != 1 << 16:
        die(f"DRC wrote a {len(taps)}-tap filter, expected 65536")
    # Largest gain at any frequency, plus 1 dB for what falls between FFT bins.
    headroom = max(max(spectrum_db(taps)), 0.0) + 1.0
    # A new file name each time, so CamillaDSP sees a changed config and reloads.
    filter_path = COEFFS / f"room-eq-{time.strftime('%Y%m%d-%H%M%S')}.pcm"
    shutil.copyfile(WORK / "filter.pcm", filter_path)
    live = install_config(f"DRC {args.strength}, {time.strftime('%Y-%m-%d %H:%M')}",
                          filter_path, headroom)

    after = None
    if live and not args.reuse:
        print("==> Measuring the result through the correction")
        time.sleep(3)  # let CamillaDSP switch to the new filter
        try:
            sweep_and_record("verify", EQ_SINK, mic=mic)
            after = response(*impulse_response("verify"), cal)
        except SystemExit as e:
            print(f"warning: the correction is installed, but measuring it failed ({e.code}); "
                  "is the desktop volume very low?", file=sys.stderr)
    report(before, after)
    print(f'Done. The correction is live on "KH 120A (room EQ)"; `{Path(sys.argv[0]).name} --flat` removes it.')


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     epilog="See the top of this file for the full instructions.")
    parser.add_argument("--strength", choices=("minimal", "soft", "normal", "strong", "extreme"),
                        default="normal", help="DRC profile (default normal)")
    parser.add_argument("--target", default=HERE / "target.txt", help="target curve (default target.txt)")
    parser.add_argument("--cal", default=HERE / "emm6-38048.txt", help="mic calibration file (freq/dB text, on-axis)")
    parser.add_argument("--reuse", action="store_true", help="recompute from the last sweep instead of measuring")
    parser.add_argument("--flat", action="store_true", help="remove the correction")
    parser.add_argument("--pause", action="store_true", help="wait for Enter before exiting (for the launcher)")
    args = parser.parse_args()
    status = 0
    try:
        if args.flat:
            install_config("flat")
        else:
            calibrate(args)
    except SystemExit as e:
        status = e.code
    except Exception as e:  # keep the launcher's window open whatever went wrong
        status = f"error: {e!r}"
    except KeyboardInterrupt:
        status = "interrupted"
    if args.pause:
        if status:
            print(status, file=sys.stderr)
            status = 1
        input("Press Enter to close...")
    sys.exit(status)


if __name__ == "__main__":
    main()
