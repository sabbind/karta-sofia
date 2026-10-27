"""BGS2005 / CCS2005 (Lambert Conformal Conic 2SP, GRS80) -> WGS84 lon/lat."""
import math

A = 6378137.0
F_INV = 298.257222101
E2 = (2 - 1 / F_INV) / F_INV
E = math.sqrt(E2)

LAT0 = math.radians(42.6678756833)
LON0 = math.radians(25.5)
LAT1 = math.radians(42.0)
LAT2 = math.radians(43.33333333333334)
FE = 500000.0
FN = 4725824.3591


def _m(p):
    return math.cos(p) / math.sqrt(1 - E2 * math.sin(p) ** 2)


def _t(p):
    s = math.sin(p)
    return math.tan(math.pi / 4 - p / 2) / ((1 - E * s) / (1 + E * s)) ** (E / 2)


M1, M2 = _m(LAT1), _m(LAT2)
T1, T2, T0 = _t(LAT1), _t(LAT2), _t(LAT0)
N = (math.log(M1) - math.log(M2)) / (math.log(T1) - math.log(T2))
FF = M1 / (N * T1 ** N)
R0 = A * FF * T0 ** N


def inverse(x, y):
    dx = x - FE
    dy = R0 - (y - FN)
    r = math.copysign(math.hypot(dx, dy), N)
    t = (r / (A * FF)) ** (1 / N)
    theta = math.atan2(dx, dy)
    lon = theta / N + LON0
    lat = math.pi / 2 - 2 * math.atan(t)
    for _ in range(8):
        s = math.sin(lat)
        lat = math.pi / 2 - 2 * math.atan(t * ((1 - E * s) / (1 + E * s)) ** (E / 2))
    return math.degrees(lon), math.degrees(lat)


def forward(lon, lat):
    p = math.radians(lat)
    r = A * FF * _t(p) ** N
    theta = N * (math.radians(lon) - LON0)
    return FE + r * math.sin(theta), FN + R0 - r * math.cos(theta)
