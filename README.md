# Bazzite tools

Small setup scripts I use on [Bazzite](https://bazzite.gg). Each folder is
independent; run its `install.sh` as your normal desktop user.

| Folder | What it sets up |
| --- | --- |
| [`shairport-sync/`](shairport-sync/) | AirPlay 2 receiver (Shairport Sync) as a rootful Podman Quadlet playing into the desktop's PipeWire session |
| [`room-eq/`](room-eq/) | System-wide room correction with CamillaDSP and DRC-FIR, calibrated with one command |

## shairport-sync

```
./shairport-sync/install.sh [airplay-name]
```

Details and removal steps are at the top of the script.

## room-eq

For one speaker (Neumann KH 120A) on one output of a Focusrite Scarlett,
measured with a Dayton EMM-6 on its mic input.

```
./room-eq/install.sh [left|right]
```

The argument is the output the speaker is plugged into (default `right`).

This installs, all under your home directory:

- a virtual output, **KH 120A (room EQ)**, made the default so the desktop and
  AirPlay play into it;
- [CamillaDSP](https://github.com/HEnquist/camilladsp) as a user service, which
  mixes that output down to mono, applies the correction and plays it on the
  speaker's output only, and a small service that keeps it connected to both;
- [CamillaGUI](https://github.com/HEnquist/camillagui-backend) at
  <http://localhost:5005> for level meters, volume and a look at the pipeline;
- a small container with [DRC-FIR](https://drc-fir.sourceforge.net/), which
  computes the correction filter;
- a **Calibrate room EQ** entry in the application menu.

### Calibrating

1. Mic on the XLR input with 48V on, INST and AIR off, **Direct Monitor off**.
   Put it at ear height at the listening position, pointing at the speaker.
   Keep the room quiet and stop other playback. Start with the speaker volume
   low.
2. Run `./room-eq/calibrate.py` or click **Calibrate room EQ**.

It plays a 20 second sweep, computes a 65536-tap correction filter, makes it
live, then sweeps again through the correction and prints the response before
and after, so you can see what was achieved.

### Tuning

- `--strength minimal|soft|normal|strong|extreme` picks how much of the room
  DRC corrects. Stronger is flatter at the mic position and worse away from it.
- `room-eq/target.txt` is the target curve (`Hz dB` points). It is flat above
  65 Hz and follows the speaker's own roll-off below, so the filter never
  forces bass the speaker cannot produce. Edit it for a house curve.
- `./room-eq/calibrate.py --reuse --strength strong` recomputes from the last
  sweep without measuring again; `--flat` removes the correction. Switching
  between `--flat` and `--reuse` is the way to compare before and after by ear
  (the corrected sound is a few dB quieter, to leave room for the filter).
- `--cal` takes another mic's calibration file; the default,
  `room-eq/emm6-38048.txt`, is the on-axis file for my EMM-6.

The active config is `~/camilladsp/configs/room-eq.yml`. The chain adds
some delay (the filter alone is about 20 ms), which video players do not
compensate for. To bypass everything, pick the Scarlett itself as the output
device.
