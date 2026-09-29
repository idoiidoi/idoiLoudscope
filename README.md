# Loudscope

Right-click an audio file in Finder to see its loudness, or nudge its gain — without opening a DAW or RX.

- **Loudscope: Analyze Loudness** — shows a dialog with integrated loudness (LUFS), loudness range (LU), true peak (dBTP), sample peak and the gain needed for −14 LUFS. **Show Graph** opens a report with the waveform and short-term / momentary loudness over time; **Change Gain…** goes straight to the gain menu.
- The summary is also written to the file's **Finder comment**, so it shows up in Get Info (⌘I), in Finder's list view (Comments column) and in Spotlight search. Existing comments are kept; only the `Loudscope:` line is replaced. The first time, macOS asks to allow controlling Finder. To turn this off, set `"finder_comment": false` in `~/Library/Application Support/Loudscope/config.json`.
- **Loudscope: Change Gain…** — ±1–6 dB, a custom value, match −14 / −16 / −23 LUFS, or set true peak to −1 dBTP. Writes a new file next to the original (`song_+3.0dB.wav`, `song_-14LUFS.mp3`). If the result would exceed −1 dBTP, it asks whether to apply a 4× oversampled limiter.

Works on WAV, AIFF, FLAC, MP3, AAC/M4A, ALAC. Output keeps the input format and bit depth; 16-bit output is dithered; lossy formats are re-encoded (MP3 320 kbps, AAC ≥ 256 kbps). Multiple files can be selected at once.

Measurements come from ffmpeg's `ebur128` filter (ITU-R BS.1770 / EBU R128).

## Install

Requirements: macOS, Python 3.9+, ffmpeg.

```sh
brew install ffmpeg            # skip if ffmpeg is already installed
git clone https://github.com/idoiidoi/idoiLoudscope.git
cd idoiLoudscope
python3 loudscope.py install
python3 loudscope.py doctor    # should end with "All good."
```

Then right-click an audio file in Finder → **Quick Actions** → **Loudscope: …**

If the items don't appear right away, restart Finder with `killall Finder`.

`install` records the absolute paths of the Python interpreter that ran it and of ffmpeg, because Finder Quick Actions run with a minimal `PATH`. If you later remove that Python or move ffmpeg, run `install` again.

### Notes for coding agents

If a user hands you this repository and asks you to install it:

1. Confirm `ffmpeg` and `ffprobe` are on `PATH`; if not, `brew install ffmpeg` (install Homebrew first if needed, with the user's consent).
2. Use a real Python 3.9+. On a Mac without Xcode Command Line Tools, `/usr/bin/python3` is only a stub that opens an install dialog — use Homebrew's `python3` instead in that case.
3. Run `python3 loudscope.py install`, then `python3 loudscope.py doctor`. Success is exit code 0 and the last line `All good.`
4. Tell the user where to find the Quick Actions (Finder right-click → Quick Actions).

To update: `git pull && python3 loudscope.py install`. To remove: `python3 loudscope.py uninstall`.

## Command line

The same script works from a terminal:

```sh
python3 loudscope.py analyze song.wav other.mp3        # summary dialog (--html: report, --text: print)
python3 loudscope.py gain --db 3 song.wav              # +3 dB
python3 loudscope.py gain --lufs -14 --limit *.wav     # match -14 LUFS, limit true peak if needed
python3 loudscope.py gain --tp -1 song.flac            # set true peak to -1 dBTP
```

`--quiet` suppresses dialogs, notifications and the browser. Without `--limit` / `--no-limit`, `gain` asks what to do when the true peak would exceed −1 dBTP (with `--quiet` it exports as is).

## Files it touches

| Path | What |
| --- | --- |
| `~/Library/Services/Loudscope: *.workflow` | the two Quick Actions |
| `~/Library/Application Support/Loudscope/` | installed script and `config.json` |
| `~/Library/Caches/Loudscope/` | HTML reports (latest 30) and `loudscope.log` |

`uninstall` removes all three.

## Limitations

- Loudness values are rounded to 0.1 dB by ffmpeg.
- The limiter is ffmpeg's `alimiter` run at 4× the sample rate — a practical true-peak limiter, not a mastering-grade one. For critical masters, use your usual tools.
- MP3 export needs an ffmpeg build with `libmp3lame` (Homebrew's has it).

## License

MIT
