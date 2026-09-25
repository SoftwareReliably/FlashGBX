# FlashGBX  # noqa: N999
# Author: Lesserkuma (github.com/Lesserkuma)

import datetime

from .i18n import __, ___, c__, c___, format_decimal, format_number


class Formatter:
    @classmethod
    def round2(cls, num: float, decimals: int = 2) -> float:
        x = pow(10, decimals)
        return int(num * x) / x

    @classmethod
    def file_size(
        cls, size: int, as_int: bool = False, space: str = " ", short: bool = False, localized: bool = True
    ) -> str:
        _translate = __ if localized else (lambda x: x)
        if size == 1:
            if short:
                return c___("Bytes (short form)", "B", "B", n=1)
            return _translate(" Byte").replace(" ", space)
        if size < 1024:
            if short:
                return f"{size:d}" + c___("Bytes (short form)", "B", "B", n=size)
            return f"{size:d}" + _translate(" Bytes").replace(" ", space)
        if size < 1024 * 1024:
            val: float = cls.round2(size / 1024)
            precision = 0 if as_int else 1
            return format_decimal(val, precision=precision, localized=localized) + _translate(" KiB").replace(
                " ", space
            )
        val = cls.round2(size / 1024 / 1024)
        precision = 0 if as_int else 2
        return format_decimal(val, precision=precision, localized=localized) + _translate(" MiB").replace(" ", space)

    @classmethod
    def progress_time_short(cls, sec: int) -> str:
        sec = sec % (24 * 3600)
        hr: int = sec // 3600
        sec %= 3600
        minute: int = sec // 60
        sec %= 60
        return f"{int(hr):02d}:{int(minute):02d}:{int(sec):02d}"

    @classmethod
    def progress_time(cls, seconds: float, as_float: bool = False, localized: bool = True) -> str:
        seconds = max(seconds, 0)

        days: int = int(seconds // 86400)
        remaining: float = seconds % 86400
        hours: int = int(remaining // 3600)
        remaining = remaining % 3600
        minutes: int = int(remaining // 60)
        secs: float = remaining % 60

        components: list[tuple[str, int]] = [("day", days), ("hour", hours), ("minute", minutes)]
        parts = []
        for name, value in components:
            if value > 0:
                parts.append(cls._format_duration_component(name, value, localized))

        if (len(parts) == 0) or (int(secs) != 0) or (seconds < 1 and as_float):
            if seconds < 1 and as_float:
                secs_formatted: str = format_decimal(secs, precision=2)
                n_value: int = int(secs)
            else:
                secs_int = int(secs)
                secs_formatted = format_number(secs_int)
                n_value = secs_int
            parts.append(
                ___("{seconds} second", "{seconds} seconds", n=n_value, seconds=secs_formatted)
                if localized
                else ("{seconds} second" if n_value == 1 else "{seconds} seconds").format(
                    n=n_value,
                    seconds=secs_formatted,
                ),
            )

        separator: str = c__("Time duration separator (e.g. 6 minutes, 4 seconds)", ", ") if localized else ", "
        return separator.join(parts)

    @staticmethod
    def _format_duration_component(name: str, value: int, localized: bool) -> str:
        singular = "{" + name + "} " + name
        plural = singular + "s"
        formatted = format_number(value)
        if localized:
            return ___(singular, plural, n=value, **{name: formatted})
        return (singular if value == 1 else plural).format(n=value, **{name: formatted})

    @classmethod
    def validate_datetime(cls, string: str, fmt: str) -> bool:
        try:
            formatted: str = datetime.datetime.strptime(string, fmt).replace(tzinfo=datetime.UTC).strftime(fmt)
        except ValueError:
            return False
        return string == formatted

    @classmethod
    def title(cls, title: str | None) -> str:
        if title is None:
            return ""
        return str(title).replace("\r\n", "␤").replace("\n", "␤").replace("\r", "␤")
