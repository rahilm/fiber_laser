/*
 * mlaser._native._emitter, geometry part (compiled into the same extension module as
 * _emitter.c): the Python ports of
 *
 *   - mlaser.parity_ports.crtmath (musl acos / atan / atan2 / hypot as Wine's msvcr100 runs
 *     them),
 *   - mlaser.parity_ports.cadmodule.curvetable (the openNURBS sample table of CContoutSmooth's
 *     curve pieces: analyser_samples, CurveTable._build / _smooth_radii, the node selection and
 *     the arc length -> XY lookups),
 *   - the per-sample point lookup of mlaser.parity_ports.cadmodule.smooth (SmoothedPath.at ->
 *     ContourSmoother._locate -> piece.point),
 *   - mlaser.parity_ports.cadmodule.ordering.ring_sort (CRingSort, modes 6 / 7),
 *
 * in C, operation for operation.  The Python ports are the reference; the same rules as
 * _emitter.c hold (docs/dev/NATIVE_KERNEL.md): -O2 -ffp-contract=off -fno-fast-math, no
 * -march; every expression is evaluated as Python evaluates it, left to right, one IEEE
 * binary64 operation at a time, no fused multiply-add; no fmod; integer parity tests.
 * Whenever Python would raise (a ZeroDivisionError, math.sqrt of a negative number,
 * math.cos(inf), int(nan), ...) or an input is outside what this file reproduces exactly
 * (an int coordinate, a piece type it does not know), the function declines (returns None to
 * its wrapper, mlaser/parity_ports/profiles/native_smooth.py) and the Python port runs
 * instead, so errors are the port's errors.
 *
 * sin / cos are the platform libm's, as Python's math.sin / math.cos call them; they are
 * called through volatile function pointers so that the compiler cannot merge a sin / cos
 * pair into one sincos call or evaluate them with its own builtins.
 *
 * Licence: MIT (same as mlaser).
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <float.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

#if defined(__FAST_MATH__)
#error "mlaser._native must not be built with -ffast-math / -Ofast (bit-identity)"
#endif
#if defined(FLT_EVAL_METHOD) && FLT_EVAL_METHOD != 0 && FLT_EVAL_METHOD != -1
#error "mlaser._native needs FLT_EVAL_METHOD == 0 (SSE2 / AArch64 double arithmetic)"
#endif
#if defined(__clang__)
#pragma STDC FP_CONTRACT OFF
#endif
#if defined(_MSC_VER)
#pragma float_control(precise, on)
#pragma fp_contract(off)
#endif

#include "_geom.h"

static double (*volatile libm_sin)(double) = sin;
static double (*volatile libm_cos)(double) = cos;

#define DECLINE (-1)

/* ============================================================================ crtmath */
static unsigned long long dbits(double x)
{
    unsigned long long b;
    memcpy(&b, &x, 8);
    return b;
}

static double bitsd(unsigned long long b)
{
    double x;
    memcpy(&x, &b, 8);
    return x;
}

#define HI(x) ((unsigned int)(dbits(x) >> 32))
#define LO(x) ((unsigned int)(dbits(x) & 0xFFFFFFFFULL))

static const double PIO2_HI = 1.57079632679489655800e+00, PIO2_LO = 6.12323399573676603587e-17;
static const double PS0 = 1.66666666666666657415e-01, PS1 = -3.25565818622400915405e-01,
                    PS2 = 2.01212532134862925881e-01, PS3 = -4.00555345006794114027e-02,
                    PS4 = 7.91534994289814532176e-04, PS5 = 3.47933107596021167570e-05;
static const double QS0 = -2.40339491173441421878e+00, QS1 = 2.02094576023350569471e+00,
                    QS2 = -6.88283971605453293030e-01, QS3 = 7.70381505559019352791e-02;

static double crt_R(double z)
{
    double p = z * (PS0 + z * (PS1 + z * (PS2 + z * (PS3 + z * (PS4 + z * PS5)))));
    double q = 1.0 + z * (QS0 + z * (QS1 + z * (QS2 + z * QS3)));
    return p / q;
}

double mlaser_crt_acos(double x)
{
    unsigned int hx = HI(x), lx = LO(x);
    unsigned int ix = hx & 0x7FFFFFFFU;
    if (ix >= 0x3FF00000U) {
        if (((ix - 0x3FF00000U) | lx) == 0)
            return (hx >> 31) ? 2 * PIO2_HI : 0.0;
        return NAN;
    }
    if (ix < 0x3FE00000U) {
        if (ix <= 0x3C600000U)
            return PIO2_HI;
        return PIO2_HI - (x - (PIO2_LO - x * crt_R(x * x)));
    }
    if (hx >> 31) {
        double z = (1.0 + x) * 0.5;
        double s = sqrt(z);
        double w = crt_R(z) * s - PIO2_LO;
        return 2 * (PIO2_HI - (s + w));
    }
    double z = (1.0 - x) * 0.5;
    double s = sqrt(z);
    double df = bitsd(dbits(s) & 0xFFFFFFFF00000000ULL);
    double c = (z - df * df) / (s + df);
    double w = crt_R(z) * s + c;
    return 2 * (df + w);
}

static const double ATANHI[4] = {4.63647609000806093515e-01, 7.85398163397448278999e-01,
                                 9.82793723247329054082e-01, 1.57079632679489655800e+00};
static const double ATANLO[4] = {2.26987774529616870924e-17, 3.06161699786838301793e-17,
                                 1.39033110312309984516e-17, 6.12323399573676603587e-17};
static const double AT[11] = {3.33333333333329318027e-01, -1.99999999998764832476e-01,
                              1.42857142725034663711e-01, -1.11111104054623557880e-01,
                              9.09088713343650656196e-02, -7.69187620504482999495e-02,
                              6.66107313738753120669e-02, -5.83357013379057348645e-02,
                              4.97687799461593236017e-02, -3.65315727442169155270e-02,
                              1.62858201153657823623e-02};

double mlaser_crt_atan(double x)
{
    unsigned int ix = HI(x);
    unsigned int sign = ix >> 31;
    int k;
    ix &= 0x7FFFFFFFU;
    if (ix >= 0x44100000U) {
        if (x != x)
            return x;
        return sign ? -ATANHI[3] : ATANHI[3];
    }
    if (ix < 0x3FDC0000U) {
        if (ix < 0x3E400000U)
            return x;
        k = -1;
    } else {
        x = fabs(x);
        if (ix < 0x3FF30000U) {
            if (ix < 0x3FE60000U) {
                k = 0;
                x = (2.0 * x - 1.0) / (2.0 + x);
            } else {
                k = 1;
                x = (x - 1.0) / (x + 1.0);
            }
        } else if (ix < 0x40038000U) {
            k = 2;
            x = (x - 1.5) / (1.0 + 1.5 * x);
        } else {
            k = 3;
            x = -1.0 / x;
        }
    }
    double z = x * x;
    double w = z * z;
    double s1 = z * (AT[0] + w * (AT[2] + w * (AT[4] + w * (AT[6] + w * (AT[8] + w * AT[10])))));
    double s2 = w * (AT[1] + w * (AT[3] + w * (AT[5] + w * (AT[7] + w * AT[9]))));
    if (k < 0)
        return x - x * (s1 + s2);
    z = ATANHI[k] - (x * (s1 + s2) - ATANLO[k] - x);
    return sign ? -z : z;
}

static const double CRT_PI = 3.1415926535897931160e+00, CRT_PI_LO = 1.2246467991473531772e-16;

double mlaser_crt_atan2(double y, double x)
{
    if (x != x || y != y)
        return x + y;
    unsigned int ix = HI(x), lx = LO(x), iy = HI(y), ly = LO(y);
    if (((ix - 0x3FF00000U) | lx) == 0)
        return mlaser_crt_atan(y);
    unsigned int m = ((iy >> 31) & 1U) | ((ix >> 30) & 2U);
    ix &= 0x7FFFFFFFU;
    iy &= 0x7FFFFFFFU;
    if ((iy | ly) == 0)
        return m < 2 ? y : (m == 2 ? CRT_PI : -CRT_PI);
    if ((ix | lx) == 0)
        return (m & 1U) ? -CRT_PI / 2 : CRT_PI / 2;
    if (ix == 0x7FF00000U) {
        if (iy == 0x7FF00000U) {
            const double t[4] = {CRT_PI / 4, -CRT_PI / 4, 3 * CRT_PI / 4, -3 * CRT_PI / 4};
            return t[m];
        }
        const double u[4] = {0.0, -0.0, CRT_PI, -CRT_PI};
        return u[m];
    }
    /* Python ints: ix + (64 << 20) < iy has no unsigned wrap-around (ix < 0x7FF00000 here) */
    if ((unsigned long long)ix + (64ULL << 20) < (unsigned long long)iy || iy == 0x7FF00000U)
        return (m & 1U) ? -CRT_PI / 2 : CRT_PI / 2;
    double z;
    if ((m & 2U) && (unsigned long long)iy + (64ULL << 20) < (unsigned long long)ix)
        z = 0.0;
    else
        z = mlaser_crt_atan(fabs(y / x));
    if (m == 0)
        return z;
    if (m == 1)
        return -z;
    if (m == 2)
        return CRT_PI - (z - CRT_PI_LO);
    return (z - CRT_PI_LO) - CRT_PI;
}

static const double SPLIT = 134217729.0;   /* 2^27 + 1 */

static void crt_sq(double x, double *hi, double *lo)
{
    double xc = x * SPLIT;
    double xh = x - xc + xc;
    double xl = x - xh;
    *hi = x * x;
    *lo = xh * xh - *hi + 2 * xh * xl + xl * xl;
}

double mlaser_crt_hypot(double x, double y)
{
    x = fabs(x);
    y = fabs(y);
    if (x < y) {
        double t = x;
        x = y;
        y = t;
    }
    if (isinf(x) || isinf(y))
        return INFINITY;
    if (x != x || y != y)
        return x + y;
    if (y == 0.0)
        return x;
    int ex = (int)((HI(x) >> 20) & 0x7FFU);
    int ey = (int)((HI(y) >> 20) & 0x7FFU);
    if (ex - ey > 64)
        return x + y;
    double z = 1.0;
    /* 2^700 and 2^-700, exact (no hex float literals: MSVC C) */
    const double up = ldexp(1.0, 700), down = ldexp(1.0, -700);
    if (ex > 0x3FF + 510) {
        z = up;
        x = x * down;
        y = y * down;
    } else if (ey < 0x3FF - 450) {
        z = down;
        x = x * up;
        y = y * up;
    }
    double hx, lx, hy, ly;
    crt_sq(x, &hx, &lx);
    crt_sq(y, &hy, &ly);
    return z * sqrt(ly + lx + hy + hx);
}

/* Python-facing: crt_hypot(x, y) etc. on two / one float (exact float type: the port computes
 * some int arguments differently, its wrapper keeps those) */
static int two_floats(PyObject *args, double *a, double *b)
{
    if (PyTuple_GET_SIZE(args) != 2 || !PyFloat_CheckExact(PyTuple_GET_ITEM(args, 0))
        || !PyFloat_CheckExact(PyTuple_GET_ITEM(args, 1))) {
        PyErr_SetString(PyExc_TypeError, "expected two floats");
        return -1;
    }
    *a = PyFloat_AS_DOUBLE(PyTuple_GET_ITEM(args, 0));
    *b = PyFloat_AS_DOUBLE(PyTuple_GET_ITEM(args, 1));
    return 0;
}

static PyObject *g_hypot(PyObject *self, PyObject *args)
{
    double a, b;
    if (two_floats(args, &a, &b) < 0)
        return NULL;
    return PyFloat_FromDouble(mlaser_crt_hypot(a, b));
}

static PyObject *g_atan2(PyObject *self, PyObject *args)
{
    double a, b;
    if (two_floats(args, &a, &b) < 0)
        return NULL;
    return PyFloat_FromDouble(mlaser_crt_atan2(a, b));
}

static PyObject *g_one(PyObject *arg, double (*fn)(double))
{
    if (!PyFloat_CheckExact(arg)) {
        PyErr_SetString(PyExc_TypeError, "expected a float");
        return NULL;
    }
    return PyFloat_FromDouble(fn(PyFloat_AS_DOUBLE(arg)));
}

static PyObject *g_atan(PyObject *self, PyObject *arg)
{
    return g_one(arg, mlaser_crt_atan);
}

static PyObject *g_acos(PyObject *self, PyObject *arg)
{
    return g_one(arg, mlaser_crt_acos);
}

/* ============================================================================ openNURBS */
#define DBL_MIN_ON 2.2250738585072014e-308
#define R_LINE 10000.0

typedef struct {
    const double *cx, *cy;   /* control points */
    const double *on;        /* openNURBS knots = full knots without the first and last */
    Py_ssize_t n;            /* control points */
} Nurbs;

/* python's a // b for b > 0 */
static long long gfloordiv(long long a, long long b)
{
    long long q = a / b;
    if ((a % b != 0) && ((a < 0) != (b < 0)))
        q -= 1;
    return q;
}

static Py_ssize_t span_index(const Nurbs *c, double t)
{
    const double *k = c->on;
    const int order = 4;
    Py_ssize_t n = c->n, last = n - order;
    if (t >= k[n - 2])
        return last;
    if (t < k[order - 1])
        return 0;
    Py_ssize_t lo = order - 2, hi = n - 1;
    while (hi - lo > 1) {
        Py_ssize_t mid = (lo + hi) / 2;   /* lo, hi >= 0 */
        if (t < k[mid])
            hi = mid;
        else
            lo = mid;
    }
    Py_ssize_t r = lo - (order - 2);
    if (r < 0)
        r = 0;
    return r < last ? r : last;
}

/* ON_EvaluateQuotientRule for dim 2 (+ w), der_count <= 2; v rows (x, y, w), out (x, y) */
static void quotient_rule(double v[3][3], int der_count, double out[3][2])
{
    double w = v[0][2];
    if (w != 0.0) {
        for (int r = 0; r <= der_count; r++)
            for (int i = 0; i < 3; i++)
                v[r][i] = v[r][i] * (1.0 / w);
        if (der_count) {
            double wp = -v[1][2];
            for (int i = 0; i < 2; i++)
                v[1][i] = v[0][i] * wp + v[1][i];
            if (der_count > 1) {
                double w2 = v[2][2];
                for (int i = 0; i < 2; i++)
                    v[2][i] = (wp + wp) * v[1][i] + v[0][i] * -w2 + v[2][i];
            }
        }
    }
    for (int r = 0; r <= der_count; r++) {
        out[r][0] = v[r][0];
        out[r][1] = v[r][1];
    }
}

static int bezier_eval(const double cvx[4], const double cvy[4], double t0, double t1, int der_count,
                       double t, double out[3][2])
{
    const int order = 4;
    if (der_count > order - 1)
        der_count = order - 1;
    int fwd = t0 == t || (t <= (t0 + t1) * 0.5 && t1 != t);
    double pts[4][3];
    for (int i = 0; i < order; i++) {
        int j = fwd ? i : order - 1 - i;
        pts[i][0] = cvx[j];
        pts[i][1] = cvy[j];
        pts[i][2] = 1.0;
    }
    double h, d, a0, a1;
    if (t1 - t0 == 0.0)
        return DECLINE;                 /* ZeroDivisionError */
    if (fwd) {
        h = t1 - t;
        d = 1.0 / (t1 - t0);
        a0 = h * d;
        a1 = (t - t0) * d;
    } else {
        h = t0 - t;
        d = 1.0 / (t1 - t0);
        a1 = (t1 - t) * d;
        a0 = (t - t0) * d;
    }
    if (a1 != 0.0) {
        for (int lvl = order - 1; lvl > 0; lvl--)
            for (int j = 0; j < lvl; j++)
                for (int k = 0; k < 3; k++)
                    pts[j][k] = pts[j + 1][k] * a1 + pts[j][k] * a0;
    }
    if (der_count) {
        double c = (double)order;
        if (h == 0.0)
            return DECLINE;
        double inv_h = 1.0 / h;
        int top = order < der_count + 1 ? order : der_count + 1;
        for (int lvl = top - 1; lvl > 0; lvl--) {
            c = c - 1.0;
            double fac = c * inv_h;
            for (int i = top - 1; i > top - 1 - lvl; i--)
                for (int k = 0; k < 3; k++)
                    pts[i][k] = (pts[i][k] - pts[i - 1][k]) * fac;
        }
    }
    double v[3][3];
    for (int i = 0; i <= der_count; i++)
        for (int k = 0; k < 3; k++)
            v[i][k] = pts[i][k];
    quotient_rule(v, der_count, out);
    return 0;
}

static void basis(const double *knot, double t, double N[16])
{
    const int order = 4, d = 3;
    for (int i = 0; i < 16; i++)
        N[i] = 0.0;
    if (knot[d] == knot[d - 1])
        return;
    N[order * order - 1] = 1.0;
    int idx = order * order - 1;
    double t_k[3], k_t[3];
    for (int j = 0; j < d; j++) {
        t_k[j] = t - knot[d - 1 - j];
        k_t[j] = knot[d + j] - t;
        int prev = idx;
        idx = idx - (order + 1);
        double x = 0.0;
        for (int r = 0; r <= j; r++) {
            double a = t_k[j - r];
            double b = k_t[r];
            double y = N[prev + r] / (b + a);
            N[idx + r] = y * b + x;
            x = a * y;
        }
        N[idx + j + 1] = x;
    }
    const double lo = 0.9999999850988388, hi = 1.0000000149011612;
    if (N[0] <= lo) {
        if (lo < N[d] && N[d] != 1.0 && N[d] < hi && N[0] == 0.0 && N[1] == 0.0 && N[2] == 0.0)
            N[d] = 1.0;
    } else if (N[0] != 1.0 && N[0] < hi && N[1] == 0.0 && N[2] == 0.0 && N[3] == 0.0) {
        N[0] = 1.0;
    }
}

/* the divisions of basis() by (b + a): zero -> ZeroDivisionError in Python */
static int basis_ok(const double *knot, double t)
{
    const int d = 3;
    if (knot[d] == knot[d - 1])
        return 0;
    double t_k[3], k_t[3];
    for (int j = 0; j < d; j++) {
        t_k[j] = t - knot[d - 1 - j];
        k_t[j] = knot[d + j] - t;
        for (int r = 0; r <= j; r++)
            if (k_t[r] + t_k[j - r] == 0.0)
                return DECLINE;
    }
    return 0;
}

static int basis_derivatives(const double *knot, int der_count, double N[16])
{
    const int order = 4, d = 3;
    double dk[4][4];
    for (int k = 0; k < der_count; k++) {
        int j = d - k;
        for (int i = 0; i < j; i++) {
            double den = knot[k + i + j] - knot[k + i];
            if (den == 0.0)
                return DECLINE;
            dk[k + 1][i] = 1.0 / den;
        }
    }
    double A[4] = {0, 0, 0, 0}, B[4] = {0, 0, 0, 0};
    double *a0 = A, *a1 = B;
    for (int i = 0; i < order; i++) {
        a0[0] = 1.0;
        for (int k = 1; k <= der_count; k++) {
            int base = order * k;
            double dN = 0.0;
            int j = k - i;
            if (j <= 0) {
                a1[0] = a0[0] * dk[k][i - k];
                dN = a1[0] * N[base + i];
                j = 1;
            }
            int jmax = d - i;
            if (jmax < k) {
                while (j <= jmax) {
                    a1[j] = (a0[j] - a0[j - 1]) * dk[k][i + j - k];
                    dN = a1[j] * N[base + i + j] + dN;
                    j++;
                }
            } else {
                while (j < k) {
                    a1[j] = (a0[j] - a0[j - 1]) * dk[k][i + j - k];
                    dN = a1[j] * N[base + i + j] + dN;
                    j++;
                }
                a1[k] = -(dk[k][i] * a0[k - 1]);
                dN = a1[k] * N[base + i + k] + dN;
            }
            N[base + i] = dN;
            double *tmp = a0;
            a0 = a1;
            a1 = tmp;
        }
    }
    double c = (double)d, dn = (double)d;
    int p = order;
    for (int r = 0; r < der_count; r++) {
        for (int i = 0; i < order; i++)
            N[p + i] = N[p + i] * c;
        p += order;
        dn -= 1.0;
        c = c * dn;
    }
    return 0;
}

/* _Nurbs.evaluate(t, der_count) -> out[0..der_count] */
static int nurbs_eval(const Nurbs *c, double t, int der_count, double out[3][2])
{
    const int order = 4;
    Py_ssize_t si = span_index(c, t);
    const double *knot = c->on + si;
    const double *cx = c->cx + si, *cy = c->cy + si;
    if (knot[order - 2] == knot[0] && knot[2 * order - 3] == knot[order - 1]) {
        double bx[4], by[4];
        for (int i = 0; i < 4; i++) {
            bx[i] = cx[i];
            by[i] = cy[i];
        }
        return bezier_eval(bx, by, knot[order - 2], knot[order - 1], der_count, t, out);
    }
    if (der_count > order - 1)
        der_count = order - 1;
    double N[16];
    if (basis_ok(knot, t) < 0)
        return DECLINE;
    basis(knot, t, N);
    if (der_count && basis_derivatives(knot, der_count, N) < 0)
        return DECLINE;
    double v[3][3];
    for (int j = 0; j <= der_count; j++) {
        double vx = 0.0, vy = 0.0, vw = 0.0;
        for (int i = 0; i < order; i++) {
            double b = N[j * order + i];
            vx = cx[i] * b + vx;
            vy = cy[i] * b + vy;
            vw = 1.0 * b + vw;
        }
        v[j][0] = vx;
        v[j][1] = vy;
        v[j][2] = vw;
    }
    quotient_rule(v, der_count, out);
    return 0;
}

static double on_length(double x, double y)
{
    double fx = fabs(x), fy = fabs(y), fz = 0.0;
    double m, o1, o2;
    if (fy >= fx && fy >= fz) {
        m = fy; o1 = fz; o2 = fx;
    } else if (fz >= fx && fz >= fy) {
        m = fz; o1 = fx; o2 = fy;
    } else {
        m = fx; o1 = fy; o2 = fz;
    }
    if (m <= DBL_MIN_ON)
        return (m > 0.0 && isfinite(m)) ? m : 0.0;
    double r = 1.0 / m;
    double a = o2 * r, b = o1 * r;
    return sqrt((1.0 + a * a) + b * b) * m;
}

static int ev_tangent(const Nurbs *c, double t, double *px, double *py, double *tx, double *ty, int *ok)
{
    double o[3][2];
    if (nurbs_eval(c, t, 1, o) < 0)
        return DECLINE;
    *px = o[0][0];
    *py = o[0][1];
    double d = on_length(o[1][0], o[1][1]);
    if (d > DBL_MIN_ON) {
        double r = 1.0 / d;
        *tx = o[1][0] * r;
        *ty = o[1][1] * r;
        *ok = 1;
    } else {
        *tx = *ty = 0.0;
        *ok = 0;
    }
    return 0;
}

static double acos_clamped(double c)
{
    return mlaser_crt_acos(c > 1.0 ? 1.0 : (c < -1.0 ? -1.0 : c));
}

static double vec_angle(double ax, double ay, double bx, double by)
{
    double dot = ax * bx + by * ay;
    double ha = mlaser_crt_hypot(ax, ay);
    double hb = mlaser_crt_hypot(bx, by);
    if (ha <= 1e-8 || hb <= 1e-8)
        return 0.0;
    return acos_clamped((dot / ha) / hb);
}

/* int(x) of Python for |x| < 2^62, else decline (int(nan) / int(inf) raise) */
static int ftol(double x, long long *out)
{
    if (!(fabs(x) < 4611686018427387904.0))
        return DECLINE;
    *out = (long long)x;
    return 0;
}

typedef struct {
    double *v;
    Py_ssize_t n, cap;
} DVec;

static int dpush(DVec *d, double x)
{
    if (d->n == d->cap) {
        Py_ssize_t cap = d->cap ? d->cap * 2 : 64;
        double *nv = PyMem_Realloc(d->v, sizeof(double) * (size_t)cap);
        if (!nv)
            return DECLINE;
        d->v = nv;
        d->cap = cap;
    }
    d->v[d->n++] = x;
    return 0;
}

#define MAX_DEPTH 200
#define MAX_SAMPLES 50000000

static int sample_span(const Nurbs *c, DVec *out, double u0, double u1, double angle, double tol, int depth)
{
    if (depth > MAX_DEPTH)
        return DECLINE;                       /* Python's recursion limit is near */
    double L = u1 - u0;
    if (L < 1e-4)
        return 0;
    double h = L * 0.01;
    if (h < 1e-4)
        h = 1e-4;
    double P0x = 0, P0y = 0, T0x = 0, T0y = 0, Pmx, Pmy, Tmx, Tmy, Pex = 0, Pey = 0, Tex = 0, Tey = 0;
    int ok = 0;
    for (int i = 0; i < 10; i++) {
        if (ev_tangent(c, i * h + u0, &P0x, &P0y, &T0x, &T0y, &ok) < 0)
            return DECLINE;
        if (ok)
            break;
        if (i == 9)
            return 0;
    }
    if (ev_tangent(c, (u0 + u1) * 0.5, &Pmx, &Pmy, &Tmx, &Tmy, &ok) < 0)
        return DECLINE;
    for (int i = 0; i < 10; i++) {
        if (ev_tangent(c, u1 - i * h, &Pex, &Pey, &Tex, &Tey, &ok) < 0)
            return DECLINE;
        if (ok)
            break;
        if (i == 9)
            return 0;
    }
    double chord = mlaser_crt_hypot(P0x - Pmx, P0y - Pmy) + mlaser_crt_hypot(Pex - Pmx, Pey - Pmy);
    double turn = vec_angle(Tmx, Tmy, Tex, Tey) + vec_angle(T0x, T0y, Tmx, Tmy);
    if (turn <= 15.0 || chord <= 5.0 * tol) {
        long long n1, n2, n3, n;
        if (angle == 0.0 || tol == 0.0)
            return DECLINE;
        if (ftol(turn / angle + 0.5, &n1) < 0 || ftol(chord / 0.005, &n2) < 0 || ftol(chord / tol + 0.5, &n3) < 0)
            return DECLINE;
        n = n1 < n2 ? n1 : n2;
        n = n > n3 ? n : n3;
        n = gfloordiv(n + 1, 2) * 2;
        if (n <= 1)
            return 0;
        if (n > MAX_SAMPLES || out->n + n > MAX_SAMPLES)
            return DECLINE;
        for (long long i = 1; i < n; i++)
            if (dpush(out, ((double)i * L) / (double)n + u0) < 0)
                return DECLINE;
        return 0;
    }
    long long m;
    if (ftol(turn / 15.0 + 0.001, &m) < 0)
        return DECLINE;
    long long n = gfloordiv(m + 2, 2) * 2;
    if (n <= 0 || n > MAX_SAMPLES)
        return DECLINE;
    double h2 = L / (double)n;
    for (long long i = 1; i <= n; i++)
        if (sample_span(c, out, u0 + (double)(i - 1) * h2, (double)i * h2 + u0, angle, tol, depth + 1) < 0)
            return DECLINE;
    return 0;
}

/* one analyser sample record [u, x, y, f, tx, ty, kx, ky] */
typedef struct {
    double u, x, y, f, tx, ty, kx, ky;
} ARec;

typedef struct {
    ARec *v;
    Py_ssize_t n, cap;
} AVec;

static int apush(AVec *a, const ARec *r)
{
    if (a->n == a->cap) {
        Py_ssize_t cap = a->cap ? a->cap * 2 : 64;
        ARec *nv = PyMem_Realloc(a->v, sizeof(ARec) * (size_t)cap);
        if (!nv)
            return DECLINE;
        a->v = nv;
        a->cap = cap;
    }
    a->v[a->n++] = *r;
    return 0;
}

/* analyser_samples(ctrl, knots, angle_deg = 2.0, tol) */
static int analyser_samples(const Nurbs *c, const double *kn, Py_ssize_t nk, double angle_deg, double tol, AVec *out)
{
    out->n = 0;
    if (nk <= 7)
        return 0;
    double ang = 0.017453292519943295 * angle_deg;
    DVec us = {NULL, 0, 0};
    AVec lst = {NULL, 0, 0};
    int rc = 0, have_first = 0;
    for (Py_ssize_t i = 4; i < nk - 3; i++) {
        us.n = 0;
        if (kn[i] - kn[i - 1] >= 1e-5) {
            if (dpush(&us, kn[i - 1]) < 0 || sample_span(c, &us, kn[i - 1], kn[i], ang, tol, 0) < 0
                || dpush(&us, kn[i]) < 0) {
                rc = DECLINE;
                goto done;
            }
        }
        /* this span's records */
        lst.n = 0;
        for (Py_ssize_t k = 0; k < us.n; k++) {
            double u = us.v[k];
            double o[3][2];
            if (nurbs_eval(c, u, 2, o) < 0) {
                rc = DECLINE;
                goto done;
            }
            double px = o[0][0], py = o[0][1], d1x = o[1][0], d1y = o[1][1], d2x = o[2][0], d2y = o[2][1];
            double ln = on_length(d1x, d1y);
            double tx, ty, kx, ky, f;
            if (ln == 0.0) {
                tx = ty = kx = ky = f = 0.0;
            } else {
                double r = 1.0 / ln;
                tx = d1x * r;
                ty = d1y * r;
                double neg = (-d2x) * tx + (-d2y) * ty;
                double lnln = ln * ln;
                if (lnln == 0.0) {
                    rc = DECLINE;
                    goto done;
                }
                f = 1.0 / lnln;
                kx = (d2x + tx * neg) * f;
                ky = (d2y + ty * neg) * f;
            }
            if (fabs(0.0 - tx) > 1e-8 || fabs(0.0 - ty) > 1e-8) {
                double hh = mlaser_crt_hypot(tx, ty);
                if (fabs(hh - 1.0) > 1e-8 && hh >= 1e-8) {
                    tx = tx / hh;
                    ty = ty / hh;
                }
            } else if (k < 1) {
                tx = 1.0;
                ty = 0.0;
            } else {
                tx = lst.v[lst.n - 1].tx;
                ty = lst.v[lst.n - 1].ty;
            }
            ARec rr = {u, px, py, f, tx, ty, kx, ky};
            if (apush(&lst, &rr) < 0) {
                rc = DECLINE;
                goto done;
            }
        }
        /* merge into out (the second and third loops of analyser_samples, span by span: the
         * first non-empty span gives out[0]) */
        if (lst.n) {
            Py_ssize_t from = 1;
            if (!have_first) {
                if (apush(out, &lst.v[0]) < 0) {
                    rc = DECLINE;
                    goto done;
                }
                have_first = 1;
            }
            for (Py_ssize_t k = from; k < lst.n; k++) {
                const ARec *r = &lst.v[k], *last = &out->v[out->n - 1];
                if (fabs(r->x - last->x) > 0.001 || fabs(r->y - last->y) > 0.001) {
                    if (apush(out, r) < 0) {
                        rc = DECLINE;
                        goto done;
                    }
                }
            }
            const ARec *last = &out->v[out->n - 1], *e = &lst.v[lst.n - 1];
            if (fabs(last->x - e->x) > 0.001 || fabs(last->y - e->y) > 0.001)
                out->v[out->n - 1] = *e;
        }
    }
done:
    PyMem_Free(us.v);
    PyMem_Free(lst.v);
    return rc;
}

/* ============================================================================ CurveTable */
static double r_of_k(double kx, double ky, int *bad)
{
    (void)bad;   /* q is a sum of squares: math.sqrt never raises here */
    if (fabs(ky) + fabs(kx) > 1500.0)
        return 0.001;
    double q = ky * ky + kx * kx;
    if (q <= 1e-8)
        return R_LINE;
    double r = 1.0 / sqrt(q);
    return r < 0.001 ? 0.001 : r;
}

static double angle_from_x(double vx, double vy)
{
    if (mlaser_crt_hypot(1.0 - vx, 0.0 - vy) < 1e-8)
        return 0.0;
    double d = mlaser_crt_atan2(vy, vx) - 0.0;
    if (3.141592653589793 < d)
        d -= 6.283185307179586;
    else if (d <= -3.141592653589793)
        d += 6.283185307179586;
    return d;
}

static void ctable_dealloc(CTable *t)
{
    PyMem_Free(t->r);
    PyMem_Free(t->ctrl);
    Py_TYPE(t)->tp_free((PyObject *)t);
}

static PyTypeObject CTableType;

static int seq_doubles(PyObject *seq, Py_ssize_t per, double **out, Py_ssize_t *n)
{
    /* a list of floats (per == 1) or a list of 2-tuples of floats (per == 2); exact float type */
    if (!PyList_Check(seq))
        return DECLINE;
    Py_ssize_t m = PyList_GET_SIZE(seq);
    double *v = PyMem_Malloc(sizeof(double) * (size_t)(m * per + 1));
    if (!v)
        return DECLINE;
    for (Py_ssize_t i = 0; i < m; i++) {
        PyObject *o = PyList_GET_ITEM(seq, i);
        if (per == 1) {
            if (!PyFloat_CheckExact(o))
                goto bad;
            v[i] = PyFloat_AS_DOUBLE(o);
        } else {
            if (!PyTuple_Check(o) || PyTuple_GET_SIZE(o) != 2 || !PyFloat_CheckExact(PyTuple_GET_ITEM(o, 0))
                || !PyFloat_CheckExact(PyTuple_GET_ITEM(o, 1)))
                goto bad;
            v[2 * i] = PyFloat_AS_DOUBLE(PyTuple_GET_ITEM(o, 0));
            v[2 * i + 1] = PyFloat_AS_DOUBLE(PyTuple_GET_ITEM(o, 1));
        }
    }
    *out = v;
    *n = m;
    return 0;
bad:
    PyMem_Free(v);
    return DECLINE;
}

/* CurveTable._build + _smooth_radii into t->r */
static int ctable_build(CTable *t, const double *ctrl, Py_ssize_t nctrl, const double *knots, Py_ssize_t nk,
                        double sample_tol)
{
    AVec a = {NULL, 0, 0};
    int rc = 0;
    Rec *R = NULL;
    t->r = NULL;
    t->n = 0;
    if (nk > 7 && nk != nctrl + 4)
        return DECLINE;     /* the port's slices on other lengths are not reproduced */
    if (nk > 7) {
        double *cx = PyMem_Malloc(sizeof(double) * (size_t)nctrl), *cy = PyMem_Malloc(sizeof(double) * (size_t)nctrl);
        if (!cx || !cy) {
            PyMem_Free(cx);
            PyMem_Free(cy);
            return DECLINE;
        }
        for (Py_ssize_t i = 0; i < nctrl; i++) {
            cx[i] = ctrl[2 * i];
            cy[i] = ctrl[2 * i + 1];
        }
        /* analyser_samples (order 4, nk > 7); _Nurbs takes on_knot = knots[1:-1] */
        Nurbs c = {cx, cy, knots + 1, nctrl};
        rc = analyser_samples(&c, knots, nk, 2.0, sample_tol, &a);
        PyMem_Free(cx);
        PyMem_Free(cy);
        if (rc < 0)
            goto out;
    }
    R = PyMem_Malloc(sizeof(Rec) * (size_t)(a.n + 1));
    if (!R) {
        rc = DECLINE;
        goto out;
    }
    Py_ssize_t n = 0;
    int bad = 0;
    for (Py_ssize_t i = 0; i < a.n; i++) {
        const ARec *s = &a.v[i];
        if (n) {
            const Rec *last = &R[n - 1];
            if (!(fabs(s->x - last->x) > 1e-4 || fabs(s->y - last->y) > 1e-4))
                continue;
        }
        Rec *c = &R[n++];
        memset(c, 0, sizeof(*c));
        c->u = s->u;
        c->x = s->x;
        c->y = s->y;
        c->f = s->f;
        c->tx = s->tx;
        c->ty = s->ty;
        c->kx = s->kx;
        c->ky = s->ky;
        c->s = 0.0;
        c->R = r_of_k(s->kx, s->ky, &bad);
        c->len = 0.0;
        c->arc = 0;
    }
    if (bad) {
        rc = DECLINE;
        goto out;
    }
    for (Py_ssize_t k = 1; k < n; k++) {
        Rec *p = &R[k - 1], *c = &R[k];
        double chord = mlaser_crt_hypot(p->x - c->x, p->y - c->y);
        double ravg = (p->R + c->R) * 0.5;
        c->arc = 0;
        if (!(500.0 <= ravg || chord > ravg * 0.7853981633974483)) {
            double hc = chord * 0.5;
            double dx = c->x - p->x;
            double dy = c->y - p->y;
            double cp = p->ty * dx - dy * p->tx;
            double cc = c->ty * dx - dy * c->tx;
            int direction = 0;
            if (cp > 0.0) {
                if (!(cc > 0.0))
                    direction = -1;
            } else if (cc > 0.0) {
                direction = 1;
            }
            if (ravg == 0.0) {
                rc = DECLINE;
                goto out;
            }
            if (direction && !((0.5 * hc) * hc / ravg < 1e-6)) {
                c->arc = 1;
                c->rad = ravg;
                double hq = ravg * ravg - hc * hc;
                if (hq < 0.0 || chord == 0.0) {   /* math.sqrt ValueError / ZeroDivisionError */
                    rc = DECLINE;
                    goto out;
                }
                double h = sqrt(hq);
                double sgn = direction != -1 ? 1.0 : -1.0;
                double q = h / chord;
                double X = dx * 0.0 - sgn * dy;
                double Y = dy * 0.0 + sgn * dx;
                c->cx = X * q + (p->x + c->x) * 0.5;
                c->cy = Y * q + (c->y + p->y) * 0.5;
                c->a0 = angle_from_x(-c->cx + p->x, -c->cy + p->y);
                double a1 = angle_from_x(-c->cx + c->x, -c->cy + c->y);
                if (direction == -1) {
                    if (c->a0 < a1)
                        a1 = a1 - 6.283185307179586;
                } else if (a1 < c->a0) {
                    a1 = a1 + 6.283185307179586;
                }
                c->a1 = a1;
            }
        }
        c->len = c->arc ? fabs(c->a1 - c->a0) * c->rad : chord;
    }
    if (n) {
        R[0].len = 0.0;
        R[0].s = 0.0;
    }
    for (Py_ssize_t k = 1; k < n; k++)
        R[k].s = R[k - 1].s + R[k].len;
    /* _smooth_radii */
    if (n) {
        double *avg = PyMem_Malloc(sizeof(double) * (size_t)n);
        if (!avg) {
            rc = DECLINE;
            goto out;
        }
        for (Py_ssize_t i = 0; i < n; i++) {
            double tot = R[i].R;
            long long cnt = 1;
            double dist = 0.0;
            Py_ssize_t j = i - 1;
            while (j >= 0) {
                if (2.0 <= dist || j < i - 2)
                    break;
                dist = dist + R[j + 1].len;
                tot = R[j].R + tot;
                cnt += 1;
                j -= 1;
            }
            dist = 0.0;
            j = i + 1;
            while (j < n) {
                if (2.0 <= dist || i + 2 < j)
                    break;
                dist = dist + R[j].len;
                tot = R[j].R + tot;
                cnt += 1;
                j += 1;
            }
            avg[i] = tot / (double)cnt;
        }
        for (Py_ssize_t i = 0; i < n; i++) {
            if (avg[i] == 0.0) {
                PyMem_Free(avg);
                rc = DECLINE;
                goto out;
            }
            if (0.01 <= R[i].R / avg[i])
                R[i].R = R[i].R - (R[i].R - avg[i]) * 0.75;
        }
        PyMem_Free(avg);
    }
    t->r = R;
    t->n = n;
    R = NULL;
    /* the lookup by binary search equals point_at_fraction's linear scan when s never
     * decreases (no NaN) */
    t->mono = 1;
    for (Py_ssize_t k = 0; k < t->n; k++) {
        if (!(t->r[k].s == t->r[k].s) || (k && !(t->r[k - 1].s <= t->r[k].s))) {
            t->mono = 0;
            break;
        }
    }
    t->total = t->n ? t->r[t->n - 1].s : 0.0;
out:
    PyMem_Free(a.v);
    PyMem_Free(R);
    return rc;
}

/* ct_build(ctrl, knots, sample_tol) -> CTable or None */
static PyObject *g_ct_build(PyObject *self, PyObject *args)
{
    PyObject *ctrl, *knots;
    double tol;
    if (!PyArg_ParseTuple(args, "OOd", &ctrl, &knots, &tol))
        return NULL;
    double *c = NULL, *k = NULL;
    Py_ssize_t nc = 0, nk = 0;
    if (seq_doubles(ctrl, 2, &c, &nc) < 0)
        Py_RETURN_NONE;
    if (seq_doubles(knots, 1, &k, &nk) < 0) {
        PyMem_Free(c);
        Py_RETURN_NONE;
    }
    CTable *t = PyObject_New(CTable, &CTableType);
    if (!t) {
        PyMem_Free(c);
        PyMem_Free(k);
        return NULL;
    }
    t->r = NULL;
    t->n = 0;
    t->ctrl = c;
    t->nctrl = nc;
    t->total = 0.0;
    t->mono = 0;
    int rc = ctable_build(t, c, nc, k, nk, tol);
    PyMem_Free(k);
    if (rc < 0) {
        Py_DECREF(t);
        PyErr_Clear();
        Py_RETURN_NONE;
    }
    return (PyObject *)t;
}

/* FUN_100f57d0 */
static Py_ssize_t ct_search(const CTable *t, double target)
{
    Py_ssize_t n = t->n;
    if (n < 2)
        return -1;
    Py_ssize_t lo = 0, hi = n - 1;
    while (lo + 1 < hi) {
        Py_ssize_t mid = (lo + hi) / 2;
        if (target < t->r[mid].s)
            hi = mid;
        else
            lo = mid;
    }
    return hi;
}

static int ct_radius_at(const CTable *t, double u, double *out)
{
    Py_ssize_t k = ct_search(t, t->total * u);
    if (k < 1) {
        *out = 99999.0;
        return 0;
    }
    const Rec *r1 = &t->r[k], *r0 = &t->r[k - 1];
    double d2 = r1->s - r0->s;
    double d3 = r1->s - t->total * u;
    int bad = 0;
    double a = r_of_k(r1->kx, r1->ky, &bad), b = r_of_k(r0->kx, r0->ky, &bad);
    if (bad || d2 == 0.0)
        return DECLINE;
    *out = (a * d3 + (d2 - d3) * b) / d2;
    return 0;
}

/* cos / sin of Python: ValueError for +-inf */
static int pycos_sin(double a, double *c, double *s)
{
    if (isinf(a))
        return DECLINE;
    *c = libm_cos(a);
    *s = libm_sin(a);
    return 0;
}

/* point_at_fraction(t) (FUN_100f8180) */
int mlaser_ct_point_at_fraction(const CTable *t, double tf, double *x, double *y)
{
    Py_ssize_t n = t->n;
    if (n < 2) {
        if (n) {
            *x = t->r[0].x;
            *y = t->r[0].y;
        } else {
            *x = *y = 0.0;
        }
        return 0;
    }
    double target = tf * t->total;
    Py_ssize_t k;
    if (t->mono) {
        /* first k in 1 .. n-1 with target <= s[k], else n - 1 (also for a NaN target) */
        Py_ssize_t lo = 1, hi = n;
        while (lo < hi) {
            Py_ssize_t mid = lo + (hi - lo) / 2;
            if (target <= t->r[mid].s)
                hi = mid;
            else
                lo = mid + 1;
        }
        k = lo;
    } else {
        k = 1;
        while (k < n && !(target <= t->r[k].s))
            k++;
    }
    if (k >= n)
        k = n - 1;
    const Rec *r = &t->r[k], *p = &t->r[k - 1];
    double d = r->s - t->total * tf;
    if (r->len == 0.0)
        return DECLINE;
    double f = d / r->len;
    if (!r->arc) {
        *x = (p->x - r->x) * f + r->x;
        *y = (p->y - r->y) * f + r->y;
        return 0;
    }
    double ang = r->a1 - (r->a1 - r->a0) * f;
    double c, s;
    if (pycos_sin(ang, &c, &s) < 0)
        return DECLINE;
    *x = c * r->rad + r->cx;
    *y = s * r->rad + r->cy;
    return 0;
}

/* CurveTable.point(s) with the piece's window and length */
int mlaser_ct_point(const CTable *t, double u0, double u1, double length, double s, double *x, double *y)
{
    double tf;
    if (length > 0) {
        tf = s / length;
    } else {
        tf = 0.0;
    }
    if (fabs((u1 - u0) - 1.0) >= 1e-8)
        tf = tf * (u1 - u0) + u0;
    return mlaser_ct_point_at_fraction(t, tf, x, y);
}

static int ct_point_f(const CTable *t, double u0, double u1, double f, double *x, double *y)
{
    double tt = (u1 - u0) * f + u0;
    Py_ssize_t k = ct_search(t, t->total * tt);
    if (k < 1) {
        if (t->nctrl) {
            *x = t->ctrl[0];
            *y = t->ctrl[1];
        } else {
            *x = *y = 0.0;
        }
        return 0;
    }
    const Rec *r = &t->r[k], *p = &t->r[k - 1];
    double d = r->s - t->total * tt;
    if (r->len == 0.0)
        return DECLINE;
    if (!r->arc) {
        double g = d / r->len;
        *x = r->x - (r->x + -p->x) * g;
        *y = r->y - (-p->y + r->y) * g;
        return 0;
    }
    double ang = r->a1 - (r->a1 - r->a0) * (d / r->len);
    double c, s;
    if (pycos_sin(ang, &c, &s) < 0)
        return DECLINE;
    *x = c * r->rad + r->cx;
    *y = s * r->rad + r->cy;
    return 0;
}

static int ct_tangent_f(const CTable *t, double u0, double u1, double f, double *x, double *y)
{
    if (t->n < 2) {
        *x = 1.0;
        *y = 0.0;
        return 0;
    }
    double tt = (u1 - u0) * f + u0;
    Py_ssize_t k = ct_search(t, t->total * tt);
    const Rec *r = &t->r[k];
    if (r->len == 0.0)
        return DECLINE;
    if (!r->arc) {
        double inv = 1.0 / r->len;
        *x = (r->x - t->r[k - 1].x) * inv;
        *y = (r->y - t->r[k - 1].y) * inv;
        return 0;
    }
    double ff = (r->s - t->total * tt) / r->len;
    if (ff < 0.2) {
        *x = r->tx;
        *y = r->ty;
        return 0;
    }
    if (ff > 0.8) {
        *x = t->r[k - 1].tx;
        *y = t->r[k - 1].ty;
        return 0;
    }
    double dd = r->a1 - r->a0;
    double sg = dd >= 0.0 ? 1.0 : -1.0;
    double ang = r->a1 - dd * ff;
    double c, s;
    if (pycos_sin(ang, &c, &s) < 0)
        return DECLINE;
    *x = -s * sg;
    *y = sg * c;
    return 0;
}

static PyObject *pair(int rc, double x, double y)
{
    if (rc < 0)
        Py_RETURN_NONE;
    return Py_BuildValue("(dd)", x, y);
}

static PyObject *ct_m_point_at_fraction(CTable *t, PyObject *arg)
{
    double tf = PyFloat_AsDouble(arg);
    if (tf == -1.0 && PyErr_Occurred())
        return NULL;
    double x = 0.0, y = 0.0;
    int rc = mlaser_ct_point_at_fraction(t, tf, &x, &y);
    return pair(rc, x, y);
}

static PyObject *ct_m_point(CTable *t, PyObject *args)
{
    double u0, u1, length, s, x = 0.0, y = 0.0;
    if (!PyArg_ParseTuple(args, "dddd", &u0, &u1, &length, &s))
        return NULL;
    int rc = mlaser_ct_point(t, u0, u1, length, s, &x, &y);
    return pair(rc, x, y);
}

static PyObject *ct_m_point_f(CTable *t, PyObject *args)
{
    double u0, u1, f, x = 0.0, y = 0.0;
    if (!PyArg_ParseTuple(args, "ddd", &u0, &u1, &f))
        return NULL;
    int rc = ct_point_f(t, u0, u1, f, &x, &y);
    return pair(rc, x, y);
}

static PyObject *ct_m_tangent_f(CTable *t, PyObject *args)
{
    double u0, u1, f, x = 0.0, y = 0.0;
    if (!PyArg_ParseTuple(args, "ddd", &u0, &u1, &f))
        return NULL;
    int rc = ct_tangent_f(t, u0, u1, f, &x, &y);
    return pair(rc, x, y);
}

static PyObject *ct_m_radius_at(CTable *t, PyObject *arg)
{
    double u = PyFloat_AsDouble(arg);
    if (u == -1.0 && PyErr_Occurred())
        return NULL;
    double r;
    if (ct_radius_at(t, u, &r) < 0)
        Py_RETURN_NONE;
    return PyFloat_FromDouble(r);
}

static PyObject *ct_m_search(CTable *t, PyObject *arg)
{
    double v = PyFloat_AsDouble(arg);
    if (v == -1.0 && PyErr_Occurred())
        return NULL;
    return PyLong_FromSsize_t(ct_search(t, v));
}

static int trend(double d)
{
    if (d <= 1e-6)
        return -1e-6 <= d ? 0 : -1;
    return 1;
}

/* nodes(u0, u1) -> [[R, s, 1.0], ...] of a curved table (the straight / empty case is the
 * wrapper's: is_straight uses math.dist) */
static PyObject *ct_m_nodes(CTable *t, PyObject *args)
{
    double u0, u1;
    if (!PyArg_ParseTuple(args, "dd", &u0, &u1))
        return NULL;
    Py_ssize_t n = t->n;
    const Rec *R = t->r;
    if (n < 1)
        Py_RETURN_NONE;
    Py_ssize_t *idx = PyMem_Malloc(sizeof(Py_ssize_t) * (size_t)(n + 2));
    Py_ssize_t *out = PyMem_Malloc(sizeof(Py_ssize_t) * (size_t)(2 * n + 4));
    double (*nl)[2] = NULL;
    PyObject *res = NULL;
    if (!idx || !out)
        goto fail;
    /* _trend_points */
    Py_ssize_t ni = 0;
    idx[ni++] = 0;
    int prev = n > 1 ? trend(R[1].R - R[0].R) : 0;
    for (Py_ssize_t k = 1; k < n - 1; k++) {
        int tr = trend(R[k + 1].R - R[k].R);
        if (tr != prev)
            idx[ni++] = k;
        prev = tr;
    }
    idx[ni++] = n - 1;
    /* _prune_points */
    Py_ssize_t k = ni - 3;
    while (k >= 2) {
        Py_ssize_t a = idx[k - 1], b = idx[k], c = idx[k + 1];
        if (R[c].s - R[b].s < 0.05 && R[b].s - R[a].s < 0.05) {
            double r2 = R[c].R, r3 = R[a].R;
            double m = r3 < r2 ? r2 : r3;
            if (fabs(r3 - r2) < m * 0.1) {
                idx[k - 1] = idx[k];
                memmove(idx + k, idx + k + 2, sizeof(Py_ssize_t) * (size_t)(ni - k - 2));
                ni -= 2;
                k -= 2;
            }
        }
        k -= 1;
    }
    /* _select_points */
    Py_ssize_t no = 0, ocap = 2 * n + 4;
    if (n >= 2) {
        out[no++] = 0;
        for (Py_ssize_t m = 1; m < ni; m++) {
            Py_ssize_t cur = idx[m], p = out[no - 1];
            if (0.1 <= R[cur].s - R[p].s) {
                for (Py_ssize_t j = p + 1; j < cur; j++) {
                    double dot = R[j - 1].tx * R[j].tx + R[j].ty * R[j - 1].ty;
                    if (3.0 < R[j].s - R[p].s || dot < 0.85 || 1.00001 < dot) {
                        if (no >= ocap)
                            goto fail;
                        out[no++] = j;
                        p = j;
                    }
                }
                if (no >= ocap)
                    goto fail;
                out[no++] = cur;
            } else if (R[cur].R < R[out[no - 1]].R) {
                out[no - 1] = cur;
            }
        }
        if (out[no - 1] < n - 1) {
            if (no >= ocap)
                goto fail;
            out[no++] = n - 1;
        }
    }
    Py_ssize_t first = 0, last = no - 1;
    if (n >= 2) {
        double total = R[n - 1].s;
        double lo = total * u0, hi = total * u1;
        while (first < no && !(lo <= R[out[first]].s))
            first++;
        while (last >= 0 && !(R[out[last]].s <= hi))
            last--;
    } else {
        first = 0;
        last = -1;
    }
    /* nodes() proper */
    nl = PyMem_Malloc(sizeof(double[2]) * (size_t)(no + 3));
    if (!nl)
        goto fail;
    Py_ssize_t nn = 0;
    double r0;
    if (ct_radius_at(t, u0, &r0) < 0)
        goto decline;
    nl[nn][0] = r0;
    nl[nn][1] = 0.0;
    nn++;
    double off = t->total * u0;
    for (Py_ssize_t q = first; q <= last; q++) {
        Py_ssize_t kk = out[q];
        double s = R[kk].s - off;
        double Rk = R[kk].R;
        if (s - nl[nn - 1][1] <= 0.001) {
            if (nl[nn - 1][0] > Rk)
                nl[nn - 1][0] = Rk;
        } else {
            nl[nn][0] = Rk;
            nl[nn][1] = s;
            nn++;
        }
    }
    double r_end;
    if (ct_radius_at(t, u1, &r_end) < 0)
        goto decline;
    double s_end = t->total * u1 - off;
    if (nn < 2 || 0.05 < s_end - nl[nn - 1][1]) {
        nl[nn][0] = r_end;
        nl[nn][1] = s_end;
        nn++;
    } else {
        nl[nn - 1][1] = s_end;
        if (r_end < nl[nn - 1][0])
            nl[nn - 1][0] = r_end;
    }
    /* _thin_and_floor(curve=True) */
    if (nn > 2) {
        for (Py_ssize_t q = 2; q < nn; q++) {
            if (nl[q][0] < 0.05 && nl[q - 1][0] < 0.05) {
                if (nl[q][0] <= nl[q - 1][0])
                    nl[q - 1][0] = 0.05;
                else
                    nl[q][0] = 0.05;
            }
        }
    }
    for (Py_ssize_t q = nn - 1; q > 0; q--)
        if (nl[q][0] < 0.001)
            nl[q][0] = 0.001;
    res = PyList_New(nn);
    if (!res)
        goto fail;
    for (Py_ssize_t q = 0; q < nn; q++) {
        PyObject *row = Py_BuildValue("[ddd]", nl[q][0], nl[q][1], 1.0);
        if (!row) {
            Py_CLEAR(res);
            goto fail;
        }
        PyList_SET_ITEM(res, q, row);
    }
    PyMem_Free(idx);
    PyMem_Free(out);
    PyMem_Free(nl);
    return res;
decline:
    PyMem_Free(idx);
    PyMem_Free(out);
    PyMem_Free(nl);
    Py_RETURN_NONE;
fail:
    PyMem_Free(idx);
    PyMem_Free(out);
    PyMem_Free(nl);
    if (!PyErr_Occurred())
        Py_RETURN_NONE;
    return NULL;
}

/* records() -> list of 17-tuples (u, x, y, s, f, tx, ty, kx, ky, R, len, arc, cx, cy, rad, a0, a1):
 * the port's _Rec values, for its fallback methods and the tests */
static PyObject *ct_m_records(CTable *t, PyObject *unused)
{
    PyObject *res = PyList_New(t->n);
    if (!res)
        return NULL;
    for (Py_ssize_t k = 0; k < t->n; k++) {
        const Rec *r = &t->r[k];
        PyObject *row = Py_BuildValue("(ddddddddddd" "N" "ddddd)", r->u, r->x, r->y, r->s, r->f, r->tx, r->ty,
                                      r->kx, r->ky, r->R, r->len, PyBool_FromLong(r->arc), r->cx, r->cy,
                                      r->rad, r->a0, r->a1);
        if (!row) {
            Py_DECREF(res);
            return NULL;
        }
        PyList_SET_ITEM(res, k, row);
    }
    return res;
}

static PyObject *ct_get_total(CTable *t, void *c)
{
    return PyFloat_FromDouble(t->total);
}

static PyObject *ct_get_n(CTable *t, void *c)
{
    return PyLong_FromSsize_t(t->n);
}

static PyMethodDef ct_methods[] = {
    {"point_at_fraction", (PyCFunction)ct_m_point_at_fraction, METH_O, "(x, y) or None"},
    {"point", (PyCFunction)ct_m_point, METH_VARARGS, "point(u0, u1, length, s) -> (x, y) or None"},
    {"point_f", (PyCFunction)ct_m_point_f, METH_VARARGS, "point_f(u0, u1, f) -> (x, y) or None"},
    {"tangent_f", (PyCFunction)ct_m_tangent_f, METH_VARARGS, "tangent_f(u0, u1, f) -> (x, y) or None"},
    {"radius_at", (PyCFunction)ct_m_radius_at, METH_O, "radius or None"},
    {"search", (PyCFunction)ct_m_search, METH_O, "FUN_100f57d0 index"},
    {"nodes", (PyCFunction)ct_m_nodes, METH_VARARGS, "nodes(u0, u1) -> [[R, s, 1.0], ...] or None"},
    {"records", (PyCFunction)ct_m_records, METH_NOARGS, "the record values"},
    {NULL, NULL, 0, NULL},
};

static PyGetSetDef ct_getset[] = {
    {"total", (getter)ct_get_total, NULL, "table length (s of the last record)", NULL},
    {"n", (getter)ct_get_n, NULL, "number of records", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyTypeObject CTableType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "mlaser._native._emitter.CTable",
    .tp_basicsize = sizeof(CTable),
    .tp_dealloc = (destructor)ctable_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = "the record table of a CurveTable (curvetable.py) in C",
    .tp_methods = ct_methods,
    .tp_getset = ct_getset,
};

/* ============================================================================ path sampling */
/*
 * sample_path(pieces, cum, ss) -> bytes of 2 * len(ss) doubles (x0, y0, x1, y1, ...), or None.
 *
 * SmoothedPath.at(s)[0] = ContourSmoother.point_at(s) for every s: ``_locate`` (bisect_right
 * over cum, skip sentinels forward, else backward, u = min(max(s - cum[i], 0.0), length)) and
 * the piece's point(u).  ``pieces``: per piece a tuple
 *   (0,)                                   sentinel
 *   (1, length, p0x, p0y, p1x, p1y)         _LineG
 *   (2, length, cx, cy, r, a0, sw)          _ArcG
 *   (3, length, ctable, u0, u1)             CurveTable (C table)
 *   (4, length, callable)                   any other piece: callable(u) -> (x, y) (called back)
 * with ``length`` = piece.length.  No real piece at all: every point is (0.0, 0.0).
 */
typedef struct {
    int kind;
    double length, a, b, c, d, e;
    CTable *t;
    PyObject *fn;
} PieceS;

static int f_item(PyObject *tup, Py_ssize_t i, double *v)
{
    PyObject *o = PyTuple_GET_ITEM(tup, i);
    if (!PyFloat_CheckExact(o))
        return DECLINE;
    *v = PyFloat_AS_DOUBLE(o);
    return 0;
}

static int piece_point(const PieceS *p, double u, double *x, double *y)
{
    switch (p->kind) {
    case 1: {            /* _LineG.point */
        if (p->length <= 0) {
            *x = p->a;
            *y = p->b;
            return 0;
        }
        double f = u / p->length;
        *x = p->a + (-p->a + p->c) * f;
        *y = f * (p->d + -p->b) + p->b;
        return 0;
    }
    case 2: {            /* _ArcG.point: a = a0 + copysign(s / r, sw) if r > 0 else a0 */
        double cx = p->a, cy = p->b, r = p->c, a0 = p->d, sw = p->e;
        double a;
        if (r > 0) {
            a = a0 + copysign(u / r, sw);
        } else {
            a = a0;
        }
        double c, s;
        if (pycos_sin(a, &c, &s) < 0)
            return DECLINE;
        *x = cx + r * c;
        *y = cy + r * s;
        return 0;
    }
    case 3:
        return mlaser_ct_point(p->t, p->a, p->b, p->length, u, x, y);
    case 4: {
        PyObject *arg = PyFloat_FromDouble(u);
        if (!arg)
            return DECLINE;
        PyObject *r = PyObject_CallOneArg(p->fn, arg);
        Py_DECREF(arg);
        if (!r)
            return DECLINE;
        int rc = DECLINE;
        if (PyTuple_Check(r) && PyTuple_GET_SIZE(r) == 2 && PyFloat_CheckExact(PyTuple_GET_ITEM(r, 0))
            && PyFloat_CheckExact(PyTuple_GET_ITEM(r, 1))) {
            *x = PyFloat_AS_DOUBLE(PyTuple_GET_ITEM(r, 0));
            *y = PyFloat_AS_DOUBLE(PyTuple_GET_ITEM(r, 1));
            rc = 0;
        }
        Py_DECREF(r);
        return rc;
    }
    }
    return DECLINE;
}

static PyObject *g_sample_path(PyObject *self, PyObject *args)
{
    PyObject *pieces, *cumo, *sso;
    if (!PyArg_ParseTuple(args, "OOO", &pieces, &cumo, &sso))
        return NULL;
    if (!PyList_Check(pieces) || !PyList_Check(cumo))
        Py_RETURN_NONE;
    Py_ssize_t np_ = PyList_GET_SIZE(pieces), ncum = PyList_GET_SIZE(cumo);
    PieceS *P = PyMem_Calloc((size_t)(np_ + 1), sizeof(PieceS));
    double *cum = PyMem_Malloc(sizeof(double) * (size_t)(ncum + 1));
    PyObject *res = NULL;
    Py_buffer sb;
    sb.obj = NULL;
    PyObject *ssf = NULL;
    if (!P || !cum)
        goto decline;
    for (Py_ssize_t i = 0; i < ncum; i++) {
        PyObject *o = PyList_GET_ITEM(cumo, i);
        if (!PyFloat_CheckExact(o))
            goto decline;
        cum[i] = PyFloat_AS_DOUBLE(o);
    }
    int has_real = 0;
    for (Py_ssize_t i = 0; i < np_; i++) {
        PyObject *t = PyList_GET_ITEM(pieces, i);
        if (!PyTuple_Check(t) || PyTuple_GET_SIZE(t) < 1)
            goto decline;
        long k = PyLong_AsLong(PyTuple_GET_ITEM(t, 0));
        if (k == -1 && PyErr_Occurred())
            goto decline;
        PieceS *p = &P[i];
        p->kind = (int)k;
        if (k == 0)
            continue;
        has_real = 1;
        Py_ssize_t m = PyTuple_GET_SIZE(t);
        if (m < 2 || f_item(t, 1, &p->length) < 0)
            goto decline;
        if (k == 1) {
            if (m != 6 || f_item(t, 2, &p->a) < 0 || f_item(t, 3, &p->b) < 0 || f_item(t, 4, &p->c) < 0
                || f_item(t, 5, &p->d) < 0)
                goto decline;
        } else if (k == 2) {
            if (m != 7 || f_item(t, 2, &p->a) < 0 || f_item(t, 3, &p->b) < 0 || f_item(t, 4, &p->c) < 0
                || f_item(t, 5, &p->d) < 0 || f_item(t, 6, &p->e) < 0)
                goto decline;
        } else if (k == 3) {
            if (m != 5 || !Py_IS_TYPE(PyTuple_GET_ITEM(t, 2), &CTableType) || f_item(t, 3, &p->a) < 0
                || f_item(t, 4, &p->b) < 0)
                goto decline;
            p->t = (CTable *)PyTuple_GET_ITEM(t, 2);
        } else if (k == 4) {
            if (m != 3)
                goto decline;
            p->fn = PyTuple_GET_ITEM(t, 2);
        } else {
            goto decline;
        }
    }
    /* ss: a buffer of doubles or a list of floats */
    Py_ssize_t ns;
    const double *sv = NULL;
    PyObject **so = NULL;
    if (PyList_Check(sso)) {
        ns = PyList_GET_SIZE(sso);
        so = PySequence_Fast_ITEMS(sso);
    } else {
        if (PyObject_GetBuffer(sso, &sb, PyBUF_FORMAT | PyBUF_C_CONTIGUOUS) < 0) {
            PyErr_Clear();
            sb.obj = NULL;
            goto decline;
        }
        const char *f = sb.format ? sb.format : "B";
        if (*f == '<' || *f == '=' || *f == '@')
            f++;
        if (sb.itemsize != 8 || strcmp(f, "d") != 0)
            goto decline;
        ns = sb.len / 8;
        sv = (const double *)sb.buf;
    }
    res = PyBytes_FromStringAndSize(NULL, (Py_ssize_t)(2 * ns * (Py_ssize_t)sizeof(double)));
    if (!res)
        goto fail;
    double *o = (double *)PyBytes_AS_STRING(res);
    for (Py_ssize_t q = 0; q < ns; q++) {
        double s;
        if (sv) {
            s = sv[q];
        } else {
            PyObject *e = so[q];
            if (!PyFloat_CheckExact(e))
                goto decline;
            s = PyFloat_AS_DOUBLE(e);
        }
        double x = 0.0, y = 0.0;
        if (has_real && np_ > 0) {
            /* bisect.bisect_right(cum, s) - 1 */
            Py_ssize_t lo = 0, hi = ncum;
            while (lo < hi) {
                Py_ssize_t mid = (lo + hi) / 2;
                if (s < cum[mid])
                    hi = mid;
                else
                    lo = mid + 1;
            }
            Py_ssize_t i = lo - 1;
            if (i < 0)
                i = 0;
            if (i > np_ - 1)
                i = np_ - 1;
            while (i < np_ && P[i].kind == 0)
                i++;
            if (i >= np_) {
                i = np_ - 1;
                while (P[i].kind == 0)
                    i--;
            }
            if (i >= ncum)
                goto decline;            /* cum shorter than the pieces: not the smoother's */
            /* min(max(s - cum[i], 0.0), length) with Python's max / min */
            double u = s - cum[i];
            if (0.0 > u)
                u = 0.0;
            if (P[i].length < u)
                u = P[i].length;
            if (piece_point(&P[i], u, &x, &y) < 0)
                goto decline;
        }
        o[2 * q] = x;
        o[2 * q + 1] = y;
    }
    if (sb.obj)
        PyBuffer_Release(&sb);
    Py_XDECREF(ssf);
    PyMem_Free(P);
    PyMem_Free(cum);
    return res;
decline:
    Py_CLEAR(res);
    if (sb.obj)
        PyBuffer_Release(&sb);
    Py_XDECREF(ssf);
    PyMem_Free(P);
    PyMem_Free(cum);
    PyErr_Clear();
    Py_RETURN_NONE;
fail:
    if (sb.obj)
        PyBuffer_Release(&sb);
    Py_XDECREF(ssf);
    PyMem_Free(P);
    PyMem_Free(cum);
    return NULL;
}

/* ============================================================================ plan sampling */
/*
 * sample_plan(blocks, dt) -> bytes of doubles, or None: Plan.sample (planner.py, CAD 0x1010a0d0
 * -> Profile.sample_from 0x101094e0) of the plan's blocks, each a tuple
 * (L, vs, vmax, ve, A, J, t0 .. t6, s0 .. s6) of floats.
 */
#define MAX_PLAN_SAMPLES 200000000

static double prof_pos(const double *B, int k, double tt, const double *e, const double *base)
{
    double J = B[5], A = B[4], vm = B[2], vs = B[1], ve = B[3];
    const double *t = B + 6, *s = B + 13;
    double u;
    switch (k) {
    case 0:
        return vs * tt + (tt * (tt * tt) * J) / 6.0;
    case 1:
        u = tt - e[0];
        return u * (J * t[0] * t[0] * 0.5 + vs) + base[1] + A * u * u * 0.5;
    case 2:
        u = t[2] - (tt - e[1]);
        return (s[2] + base[2]) - (vm * u - (J * (u * (u * u))) / 6.0);
    case 3:
        return (tt - e[2]) * vm + base[3];
    case 4:
        u = tt - e[3];
        return (vm * u + base[4]) - (u * (u * u) * J) / 6.0;
    case 5:
        u = tt - e[4];
        return (u * (vm - t[4] * J * t[4] * 0.5) + base[5]) - A * u * u * 0.5;
    default:
        u = t[6] - (tt - (e[4] + t[5]));
        return (s[6] + s[5] + base[5]) - ((u * (u * u) * J) / 6.0 + ve * u);
    }
}

static PyObject *g_sample_plan(PyObject *self, PyObject *args)
{
    PyObject *blocks;
    double dt;
    if (!PyArg_ParseTuple(args, "Od", &blocks, &dt))
        return NULL;
    if (!PyList_Check(blocks))
        Py_RETURN_NONE;
    Py_ssize_t nb = PyList_GET_SIZE(blocks);
    double *B = PyMem_Malloc(sizeof(double) * 20 * (size_t)(nb + 1));
    DVec out = {NULL, 0, 0};
    if (!B)
        return PyErr_NoMemory();
    for (Py_ssize_t i = 0; i < nb; i++) {
        PyObject *t = PyList_GET_ITEM(blocks, i);
        if (!PyTuple_Check(t) || PyTuple_GET_SIZE(t) != 20)
            goto decline;
        for (int j = 0; j < 20; j++)
            if (f_item(t, j, &B[20 * i + j]) < 0)
                goto decline;
    }
    double carry = -1.0, off = 0.0;
    for (Py_ssize_t i = 0; i < nb; i++) {
        const double *b = B + 20 * i;
        const double *t = b + 6, *s = b + 13;
        double t0 = (0.0 <= carry || dt <= carry) ? dt - carry : 0.0;
        /* Profile._ends */
        double e[7], base[7], acc = 0.0, bb = 0.0;
        for (int k = 0; k < 7; k++) {
            acc = acc + t[k];
            e[k] = acc;
        }
        base[0] = 0.0;
        for (int k = 0; k < 6; k++) {
            bb = s[k] + bb;
            base[k + 1] = bb;
        }
        double T = t[0] + t[1] + t[2] + t[3] + t[4] + t[5] + t[6];
        double tt = t0;
        Py_ssize_t n0 = out.n;
        for (int k = 0; k < 7; k++) {
            if (k < 6) {
                while (tt < e[k]) {
                    if (out.n >= MAX_PLAN_SAMPLES || dpush(&out, prof_pos(b, k, tt, e, base) + off) < 0)
                        goto decline;
                    tt = tt + dt;
                }
            } else {
                while (tt <= T) {
                    if (out.n >= MAX_PLAN_SAMPLES || dpush(&out, prof_pos(b, k, tt, e, base) + off) < 0)
                        goto decline;
                    tt = tt + dt;
                }
            }
        }
        carry = out.n > n0 ? T - (tt - dt) : carry - T;
        off = b[0] + off;
    }
    PyObject *res = PyBytes_FromStringAndSize((const char *)out.v, (Py_ssize_t)(out.n * (Py_ssize_t)sizeof(double)));
    PyMem_Free(out.v);
    PyMem_Free(B);
    return res;
decline:
    PyMem_Free(out.v);
    PyMem_Free(B);
    PyErr_Clear();
    Py_RETURN_NONE;
}

/*
 * sample_profile(block, period_ms) -> (bytes of doubles, all finite) or None: Profile.sample
 * (profile.py FUN_1000f730, the rapids' samples): positions while t <= T from t = 0, divided by
 * L, then multiplied by 1 / (last value); [] when T / period * 1000 + 0.5 < 1 or no sample.  The
 * divisions are the port's _div: IEEE (x / 0 = +-inf, 0 / 0 = nan), as C divides.
 */
static PyObject *g_sample_profile(PyObject *self, PyObject *args)
{
    PyObject *t;
    double period;
    if (!PyArg_ParseTuple(args, "Od", &t, &period))
        return NULL;
    double B[20];
    if (!PyTuple_Check(t) || PyTuple_GET_SIZE(t) != 20)
        Py_RETURN_NONE;
    for (int j = 0; j < 20; j++)
        if (f_item(t, j, &B[j]) < 0)
            Py_RETURN_NONE;
    const double *tt7 = B + 6, *s7 = B + 13;
    double T = tt7[0] + tt7[1] + tt7[2] + tt7[3] + tt7[4] + tt7[5] + tt7[6];
    double n = T / period * 1000.0 + 0.5;
    DVec out = {NULL, 0, 0};
    if (n >= 1.0) {
        double dt = period * 0.001;
        double e[7], base[7], acc = 0.0, bb = 0.0;
        for (int k = 0; k < 7; k++) {
            acc = acc + tt7[k];
            e[k] = acc;
        }
        base[0] = 0.0;
        for (int k = 0; k < 6; k++) {
            bb = s7[k] + bb;
            base[k + 1] = bb;
        }
        double tt = 0.0;
        for (int k = 0; k < 7; k++) {
            if (k < 6) {
                while (tt < e[k]) {
                    if (out.n >= MAX_PLAN_SAMPLES || dpush(&out, prof_pos(B, k, tt, e, base)) < 0)
                        goto decline;
                    tt = tt + dt;
                }
            } else {
                while (tt <= T) {
                    if (out.n >= MAX_PLAN_SAMPLES || dpush(&out, prof_pos(B, k, tt, e, base)) < 0)
                        goto decline;
                    tt = tt + dt;
                }
            }
        }
        if (out.n) {
            double L = B[0];
            for (Py_ssize_t i = 0; i < out.n; i++)
                out.v[i] = out.v[i] / L;
            double r = 1.0 / out.v[out.n - 1];
            for (Py_ssize_t i = 0; i < out.n; i++)
                out.v[i] = out.v[i] * r;
        }
    }
    int fin = 1;
    for (Py_ssize_t i = 0; i < out.n && fin; i++)
        fin = isfinite(out.v[i]) != 0;
    PyObject *b = PyBytes_FromStringAndSize((const char *)out.v, (Py_ssize_t)(out.n * (Py_ssize_t)sizeof(double)));
    PyMem_Free(out.v);
    if (!b)
        return NULL;
    return Py_BuildValue("(NO)", b, fin ? Py_True : Py_False);
decline:
    PyMem_Free(out.v);
    PyErr_Clear();
    Py_RETURN_NONE;
}

/* rapid_points(us, ax, ay, px, py) -> bytes x0, y0, ...: (a + (p - a) * u) per u (compile_items'
 * rapid_to) */
static PyObject *g_rapid_points(PyObject *self, PyObject *args)
{
    PyObject *uo;
    double ax, ay, px, py;
    if (!PyArg_ParseTuple(args, "Odddd", &uo, &ax, &ay, &px, &py))
        return NULL;
    Py_buffer ub;
    if (PyObject_GetBuffer(uo, &ub, PyBUF_FORMAT | PyBUF_C_CONTIGUOUS) < 0)
        return NULL;
    const char *f = ub.format ? ub.format : "B";
    if (*f == '<' || *f == '=' || *f == '@')
        f++;
    if (ub.itemsize != 8 || strcmp(f, "d") != 0) {
        PyBuffer_Release(&ub);
        PyErr_SetString(PyExc_TypeError, "a buffer of doubles is needed");
        return NULL;
    }
    Py_ssize_t n = ub.len / 8;
    const double *u = (const double *)ub.buf;
    PyObject *res = PyBytes_FromStringAndSize(NULL, (Py_ssize_t)(2 * n * (Py_ssize_t)sizeof(double)));
    if (res) {
        double *o = (double *)PyBytes_AS_STRING(res);
        for (Py_ssize_t i = 0; i < n; i++) {
            o[2 * i] = ax + (px - ax) * u[i];
            o[2 * i + 1] = ay + (py - ay) * u[i];
        }
    }
    PyBuffer_Release(&ub);
    return res;
}

/* ============================================================================ module part */
static PyMethodDef geom_methods[] = {
    {"crt_hypot", g_hypot, METH_VARARGS, "crtmath.hypot(x, y) of two floats"},
    {"crt_atan2", g_atan2, METH_VARARGS, "crtmath.atan2(y, x) of two floats"},
    {"crt_atan", g_atan, METH_O, "crtmath.atan(x) of a float"},
    {"crt_acos", g_acos, METH_O, "crtmath.acos(x) of a float"},
    {"ct_build", g_ct_build, METH_VARARGS, "ct_build(ctrl, knots, sample_tol) -> CTable or None"},
    {"sample_path", g_sample_path, METH_VARARGS, "sample_path(pieces, cum, ss) -> bytes or None"},
    {"sample_plan", g_sample_plan, METH_VARARGS, "sample_plan(blocks, dt) -> bytes or None"},
    {"sample_profile", g_sample_profile, METH_VARARGS, "sample_profile(block, period_ms) -> (bytes, finite) or None"},
    {"rapid_points", g_rapid_points, METH_VARARGS, "rapid_points(us, ax, ay, px, py) -> bytes"},
    {NULL, NULL, 0, NULL},
};

int mlaser_geom_exec(PyObject *m)
{
    if (PyType_Ready(&CTableType) < 0)
        return -1;
    Py_INCREF(&CTableType);
    if (PyModule_AddObject(m, "CTable", (PyObject *)&CTableType) < 0) {
        Py_DECREF(&CTableType);
        return -1;
    }
    if (PyModule_AddFunctions(m, geom_methods) < 0)
        return -1;
    if (mlaser_ring_exec(m) < 0)
        return -1;
    return 0;
}
