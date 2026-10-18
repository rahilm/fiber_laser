"""The C runtime math the original's numbers come from (acos, atan2, hypot), bit for bit.

CADModule.dll and splineAnalyerVc100.dll import ``acos``/``atan2``/``_hypot`` (and the ``_CI*``
intrinsics) from msvcr100.dll.  The parity reference runs the original under Wine, where
msvcr100.dll is Wine's builtin, whose libm is musl's (fdlibm-derived) code compiled for double
arithmetic.  These ports reproduce it exactly (checked against the harness on 4000 random
arguments each: 0 differences; Python's ``math`` differs in 6 % (acos) to 19 % (atan2) of the
cases by one unit in the last place).

Why it matters (O): CContoutSmooth's sampler FUN_100bf9c0 rounds turn/2deg + 0.5 to an integer
step count, and on symmetric corners the turn of a knot span is pi/4 up to the last bit, so the
step count (and with it every sample, node and planner point of the blend) depends on the last
bit of ``acos``.

Native msvcr100 on Windows dispatches these functions to Intel's SSE2 libm (``_CIacos`` ->
0x78ac9100) and computes ``_hypot`` on the x87 with 64-bit precision; its results can differ in
the last bit (I: not measurable here, the harness has no native CRT).
"""
from __future__ import annotations

import math
import struct

__all__ = ["acos", "atan", "atan2", "hypot"]


def _words(x: float) -> tuple[int, int]:
    b = struct.unpack("<Q", struct.pack("<d", x))[0]
    return b >> 32, b & 0xFFFFFFFF


def _clear_low_word(x: float) -> float:
    b = struct.unpack("<Q", struct.pack("<d", x))[0] & 0xFFFFFFFF00000000
    return struct.unpack("<d", struct.pack("<Q", b))[0]


_PIO2_HI = 1.57079632679489655800e+00
_PIO2_LO = 6.12323399573676603587e-17
_PS = (1.66666666666666657415e-01, -3.25565818622400915405e-01, 2.01212532134862925881e-01,
       -4.00555345006794114027e-02, 7.91534994289814532176e-04, 3.47933107596021167570e-05)
_QS = (-2.40339491173441421878e+00, 2.02094576023350569471e+00, -6.88283971605453293030e-01,
       7.70381505559019352791e-02)


def _R(z: float) -> float:
    p = z * (_PS[0] + z * (_PS[1] + z * (_PS[2] + z * (_PS[3] + z * (_PS[4] + z * _PS[5])))))
    q = 1.0 + z * (_QS[0] + z * (_QS[1] + z * (_QS[2] + z * _QS[3])))
    return p / q


def acos(x: float) -> float:
    """musl ``acos`` (src/math/acos.c)."""
    hx, lx = _words(x)
    ix = hx & 0x7FFFFFFF
    if ix >= 0x3FF00000:
        if ((ix - 0x3FF00000) | lx) == 0:
            return 2 * _PIO2_HI if hx >> 31 else 0.0
        return math.nan
    if ix < 0x3FE00000:
        if ix <= 0x3C600000:
            return _PIO2_HI
        return _PIO2_HI - (x - (_PIO2_LO - x * _R(x * x)))
    if hx >> 31:
        z = (1.0 + x) * 0.5
        s = math.sqrt(z)
        w = _R(z) * s - _PIO2_LO
        return 2 * (_PIO2_HI - (s + w))
    z = (1.0 - x) * 0.5
    s = math.sqrt(z)
    df = _clear_low_word(s)
    c = (z - df * df) / (s + df)
    w = _R(z) * s + c
    return 2 * (df + w)


_ATANHI = (4.63647609000806093515e-01, 7.85398163397448278999e-01, 9.82793723247329054082e-01,
           1.57079632679489655800e+00)
_ATANLO = (2.26987774529616870924e-17, 3.06161699786838301793e-17, 1.39033110312309984516e-17,
           6.12323399573676603587e-17)
_AT = (3.33333333333329318027e-01, -1.99999999998764832476e-01, 1.42857142725034663711e-01,
       -1.11111104054623557880e-01, 9.09088713343650656196e-02, -7.69187620504482999495e-02,
       6.66107313738753120669e-02, -5.83357013379057348645e-02, 4.97687799461593236017e-02,
       -3.65315727442169155270e-02, 1.62858201153657823623e-02)


def atan(x: float) -> float:
    """musl ``atan`` (src/math/atan.c)."""
    ix, _ = _words(x)
    sign = ix >> 31
    ix &= 0x7FFFFFFF
    if ix >= 0x44100000:
        if x != x:
            return x
        return -_ATANHI[3] if sign else _ATANHI[3]
    if ix < 0x3FDC0000:
        if ix < 0x3E400000:
            return x
        k = -1
    else:
        x = abs(x)
        if ix < 0x3FF30000:
            if ix < 0x3FE60000:
                k, x = 0, (2.0 * x - 1.0) / (2.0 + x)
            else:
                k, x = 1, (x - 1.0) / (x + 1.0)
        elif ix < 0x40038000:
            k, x = 2, (x - 1.5) / (1.0 + 1.5 * x)
        else:
            k, x = 3, -1.0 / x
    z = x * x
    w = z * z
    s1 = z * (_AT[0] + w * (_AT[2] + w * (_AT[4] + w * (_AT[6] + w * (_AT[8] + w * _AT[10])))))
    s2 = w * (_AT[1] + w * (_AT[3] + w * (_AT[5] + w * (_AT[7] + w * _AT[9]))))
    if k < 0:
        return x - x * (s1 + s2)
    z = _ATANHI[k] - (x * (s1 + s2) - _ATANLO[k] - x)
    return -z if sign else z


_PI = 3.1415926535897931160e+00
_PI_LO = 1.2246467991473531772e-16


def atan2(y: float, x: float) -> float:
    """musl ``atan2`` (src/math/atan2.c)."""
    if x != x or y != y:
        return x + y
    ix, lx = _words(x)
    iy, ly = _words(y)
    if ((ix - 0x3FF00000) | lx) == 0:
        return atan(y)
    m = ((iy >> 31) & 1) | ((ix >> 30) & 2)
    ix &= 0x7FFFFFFF
    iy &= 0x7FFFFFFF
    if (iy | ly) == 0:
        return y if m < 2 else (_PI if m == 2 else -_PI)
    if (ix | lx) == 0:
        return -_PI / 2 if m & 1 else _PI / 2
    if ix == 0x7FF00000:
        if iy == 0x7FF00000:
            return (_PI / 4, -_PI / 4, 3 * _PI / 4, -3 * _PI / 4)[m]
        return (0.0, -0.0, _PI, -_PI)[m]
    if ix + (64 << 20) < iy or iy == 0x7FF00000:
        return -_PI / 2 if m & 1 else _PI / 2
    if (m & 2) and iy + (64 << 20) < ix:
        z = 0.0
    else:
        z = atan(abs(y / x))
    if m == 0:
        return z
    if m == 1:
        return -z
    if m == 2:
        return _PI - (z - _PI_LO)
    return (z - _PI_LO) - _PI


_SPLIT = 2.0 ** 27 + 1


def _sq(x: float) -> tuple[float, float]:
    xc = x * _SPLIT
    xh = x - xc + xc
    xl = x - xh
    hi = x * x
    lo = xh * xh - hi + 2 * xh * xl + xl * xl
    return hi, lo


def hypot(x: float, y: float) -> float:
    """musl ``hypot`` (src/math/hypot.c), double evaluation."""
    x, y = abs(x), abs(y)
    if x < y:
        x, y = y, x
    if math.isinf(x) or math.isinf(y):
        return math.inf
    if x != x or y != y:
        return x + y
    if y == 0.0:
        return x
    ex = (_words(x)[0] >> 20) & 0x7FF
    ey = (_words(y)[0] >> 20) & 0x7FF
    if ex - ey > 64:
        return x + y
    z = 1.0
    if ex > 0x3FF + 510:
        z, x, y = 2.0 ** 700, x * 2.0 ** -700, y * 2.0 ** -700
    elif ey < 0x3FF - 450:
        z, x, y = 2.0 ** -700, x * 2.0 ** 700, y * 2.0 ** 700
    hx, lx = _sq(x)
    hy, ly = _sq(y)
    return z * math.sqrt(ly + lx + hy + hx)
