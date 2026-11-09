import re

_RUN = re.compile(r"^(\d{1,2})\.(\d{3})\.(\d{3})-([\dkK])$")


def valid_run(run: str) -> bool:
    found = _RUN.match(run)
    if not found:
        return False
    digits = "".join(found.group(1, 2, 3))
    total = sum(int(d) * f for d, f in zip(reversed(digits), [2, 3, 4, 5, 6, 7] * 2))
    remainder = 11 - total % 11
    expected = "0" if remainder == 11 else "K" if remainder == 10 else str(remainder)
    return found.group(4).upper() == expected
