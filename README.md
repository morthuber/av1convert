# av1convert

Convert a directory tree of videos to AV1 with FFmpeg.

## Why this exists

I created this script to ensure my collection of videos which were made over the
years with different cameras stayed accessible and playable using current video
players. 
As the files are in different formats and codecs I wanted to automate this as far
as I could.
As this primarily exists to solve my own problem, it is only tested on my own system
running arch and may cause problems elsewhere. 
Final disclaimer: I am by no means a professional developer, feel free to report
anything that looks weird or doesn't work as expected.

For further thoughts on what went into making this script, see spec.md.


## Requirements

- Python 3
- `ffmpeg` available in `PATH`

## Usage

```sh
python3 av1convert.py SOURCE_DIR [--jobs N]
```

## Options

- `SOURCE_DIR` (required): source directory containing video files
- `--jobs N` (default: number of CPU cores): number of parallel FFmpeg jobs

Examples:

```sh
# converts everyting in a relative path
python3 av1convert.py 2021/
# converts a given absolute path with special characters
python3 av1convert.py "/mnt/media/videos/year 2021/"
# converts a single file
python3 av1convert.py testvideo --jobs 4
```

## Behavior

- creates the output directory by appending `-av1` to `SOURCE_DIR`
- preserves the relative directory structure
- converts supported video files to `.mkv` with AV1 video
- removes codec tags such as `h264`, `x264`, `h265`, `x265`, `hevc`, and `avc` from filename stems
- prints one status line per file and a final summary
