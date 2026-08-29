import math
from typing import Optional

class AudioFilterBuilder:
    @staticmethod
    def build(
        speed: float = 1.0,
        bass_boost: int = 0,
        eightd: bool = False,
        crossfade_duration: float = 0.0,
        song_duration: Optional[int] = None,
        start_time: int = 0,
    ) -> str:
        """Return an FFmpeg filter string (without -af) or empty string.

        `start_time` is the playback position (in seconds, absolute within the
        song) that this FFmpeg process is starting from - i.e. whatever value
        was passed as `-ss` before `-i`. Any filter here whose timing must be
        expressed in seconds (currently just the crossfade afade filters) has
        to work in *output-relative* time, since ffmpeg resets the decoded
        stream's own clock to 0 at the seek point - it does not know or care
        about the original absolute song position. Mixing up absolute vs.
        output-relative time here was a real bug: after a seek, fades would
        fire at the wrong moment (or not at all).
        """
        filters = []

        # 1. Bass Boost: bass=g=... (filter)
        if bass_boost > 0:
            gain = bass_boost * 0.5  # 0.5 dB per step, max 5 dB
            filters.append(f"bass=g={gain}")

        # 2. 8D Audio: a genuine moving stereo pan, not the old `surround=1`
        # (which is not a real 8D effect and is not valid usage of ffmpeg's
        # `surround` upmix filter - it produced no reliable spatial effect and
        # could fail to build the filter graph at all).
        #
        # `apulsator` amplitude-modulates the left/right channels out of
        # phase with each other at a low frequency, which is exactly the
        # "rotating around your head" sensation that "8D audio" refers to.
        # `aformat=channel_layouts=stereo` guarantees the input is coerced to
        # stereo first, so this behaves safely even on a mono source instead
        # of failing to build a meaningful stereo pan.
        if eightd:
            filters.append("aformat=channel_layouts=stereo")
            filters.append("apulsator=hz=0.09")

        # 3. Speed: atempo
        if speed != 1.0 and speed > 0:
            # atempo only accepts 0.5..2.0 per instance; we clamp to that
            # range rather than chaining, since the command layer already
            # restricts input to 0.5-2.0 (see cogs/music.py's Range bound).
            clamped = max(0.5, min(2.0, speed))
            filters.append(f"atempo={clamped:.2f}")

        # 4. Crossfade: afade in/out (applied on individual songs). See the
        # docstring above - all `st=` values here are relative to start_time,
        # not absolute song position.
        if crossfade_duration > 0 and song_duration is not None:
            dur = float(song_duration)
            start_time = float(start_time)
            if dur > crossfade_duration * 2:
                if start_time < crossfade_duration:
                    fade_in_dur = min(crossfade_duration, dur - start_time)
                    if fade_in_dur > 0:
                        # We're within the fade-in window even after accounting
                        # for the seek - and since ffmpeg's own clock for this
                        # process starts at 0 regardless of start_time, the
                        # fade must start at output-relative 0, not start_time.
                        filters.append(f"afade=t=in:st=0:d={fade_in_dur:.3f}")
                end_fade_start_abs = max(start_time, dur - crossfade_duration)
                if end_fade_start_abs < dur:
                    fade_out_dur = dur - end_fade_start_abs
                    relative_fade_out_start = end_fade_start_abs - start_time
                    if fade_out_dur > 0 and relative_fade_out_start >= 0:
                        filters.append(f"afade=t=out:st={relative_fade_out_start:.3f}:d={fade_out_dur:.3f}")

        return ",".join(filters) if filters else ""
