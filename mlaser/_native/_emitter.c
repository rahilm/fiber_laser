/*
 * mlaser._native._emitter: the per-sample emitter loop of
 * mlaser.parity_ports.mainapp.sampling (Emitter.to / quantise / _step, emit_samples,
 * frames.encode_step, SpeedCurve.__call__) in C, operation for operation.
 *
 * The Python port is the reference; this file must stay bit-identical to it
 * (docs/dev/NATIVE_KERNEL.md).  Rules:
 *   - build with -O2 -ffp-contract=off -fno-fast-math (gcc / clang) or /O2 /fp:precise
 *     (MSVC), no -march=native (setup.py): every floating-point expression below is
 *     evaluated as the Python port evaluates it, one IEEE double operation at a time, no
 *     fused multiply-add; the pragmas and #error checks below repeat that in the source;
 *   - portable C99 subset that gcc, clang and MSVC (VS 2019+) all compile: long long is 64
 *     bits everywhere (LP64 and LLP64), no compiler builtins, no VLAs;
 *   - Python's int(float) is a truncation: (long long)t, guarded to |t| < 2^52;
 *   - Python's round(float) -> int is round-half-even: round_half_even() below;
 *   - math.hypot is NOT reproduced: the value comes from a callback into CPython's own
 *     math.hypot, memoised per (dx, dy) in a table owned by the Python wrapper;
 *   - any input outside the domain this file handles exactly (non-float coordinates,
 *     huge values, |step| > 127, a Python exception in a callback, ...) makes the call
 *     undo what it appended and return None: the wrapper then runs the Python reference
 *     from the unchanged state, so errors and odd inputs behave exactly as in the port.
 *
 * Licence: MIT (same as mlaser).
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <float.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

/* Deterministic IEEE binary64: refuse builds that would change the bits. */
#if defined(__FAST_MATH__)
#error "mlaser._native._emitter must not be built with -ffast-math / -Ofast (bit-identity)"
#endif
#if defined(FLT_EVAL_METHOD) && FLT_EVAL_METHOD != 0 && FLT_EVAL_METHOD != -1
/* x87 excess precision (32-bit x86 without SSE2 math): doubles would round differently */
#error "mlaser._native._emitter needs FLT_EVAL_METHOD == 0 (SSE2 / AArch64 double arithmetic)"
#endif
#if defined(__clang__)
#pragma STDC FP_CONTRACT OFF
#endif
#if defined(_MSC_VER)
/* float_control(precise, on) can re-enable contraction on some MSVC versions: fp_contract
 * off comes after it */
#pragma float_control(precise, on)
#pragma fp_contract(off)
#endif
/* long long is 64 bits (static assertion that also compiles as MSVC C without /std:c11) */
typedef char mlaser_long_long_is_64_bits[(sizeof(long long) == 8) ? 1 : -1];

#define LIMIT_T 4503599627370496.0   /* 2^52: |t| below this truncates exactly */
#define LIMIT_D 2147483647LL          /* |pulse delta| per sample */
#define LIMIT_N (1LL << 20)          /* periods one sample may be spread over */
#define LIMIT_I (1LL << 62)

typedef struct {
    double posx, posy, c0, c1, on_len;
    long long cur0, cur1, steps, overspeed;
} State;

typedef struct {
    PyObject *wire, *starts, *header;   /* borrowed */
    double *hcache;                       /* 255 x 255, NaN = not computed */
    PyObject *hyp;                        /* callable (dx, dy) -> float, borrowed */
    long long power;
    int dry;
} Out;

/* round-half-even of a double, as Python's round(x) for |x| < 2^52 */
static double round_half_even(double q)
{
    double r = floor(q);
    double diff = q - r;        /* exact */
    if (diff > 0.5)
        r += 1.0;
    else if (diff == 0.5 && fmod(r, 2.0) != 0.0)
        r += 1.0;
    return r;
}

/* floor division of Python ints (a // b) for b > 0 */
static long long floordiv(long long a, long long b)
{
    long long q = a / b;
    if ((a % b != 0) && ((a < 0) != (b < 0)))
        q -= 1;
    return q;
}

static int as_double(PyObject *o, double *out)
{
    if (PyFloat_Check(o)) {
        *out = PyFloat_AS_DOUBLE(o);
        return 0;
    }
    if (PyLong_CheckExact(o)) {
        int ovf = 0;
        long long v = PyLong_AsLongLongAndOverflow(o, &ovf);
        if (ovf || (v == -1 && PyErr_Occurred()) || v > (1LL << 53) || v < -(1LL << 53)) {
            PyErr_Clear();
            return -1;
        }
        *out = (double)v;
        return 0;
    }
    return -1;
}

static int as_ll(PyObject *o, long long *out)
{
    if (!PyLong_Check(o) || PyBool_Check(o))
        return -1;
    int ovf = 0;
    long long v = PyLong_AsLongLongAndOverflow(o, &ovf);
    if (ovf || (v == -1 && PyErr_Occurred()) || v > LIMIT_I || v < -LIMIT_I) {
        PyErr_Clear();
        return -1;
    }
    *out = v;
    return 0;
}

/* Direct-mapped cache of the int objects of the item words: a run repeats few (w1, w2)
 * values, so most steps reuse an existing int instead of allocating one (ints are immutable,
 * sharing them between wire items is invisible to Python code). */
#define WCACHE 4096
static unsigned long long wc_key[2][WCACHE];
static PyObject *wc_obj[2][WCACHE];

static PyObject *word(int which, unsigned long long v)
{
    size_t i = (size_t)((v ^ (v >> 12) ^ (v >> 24)) & (WCACHE - 1));
    PyObject *o = wc_obj[which][i];
    if (o && wc_key[which][i] == v) {
        Py_INCREF(o);
        return o;
    }
    o = PyLong_FromUnsignedLongLong(v);
    if (!o)
        return NULL;
    Py_XDECREF(wc_obj[which][i]);
    Py_INCREF(o);
    wc_obj[which][i] = o;
    wc_key[which][i] = v;
    return o;
}

/* Emitter._step + frames.encode_step.  on: the sample's laser flag; duty, freq as the port
 * holds them after int(); fclamp: freq passed explicitly (max(0, min(0xFFFF, int(freq))))
 * versus the emitter's own self.freq (no clamp, only encode_step's & 0xFFFF). */
static int do_step(Out *o, State *s, long long dx, long long dy, int on, long long d,
                   long long f, int fclamp)
{
    if (dx < -127 || dx > 127 || dy < -127 || dy > 127)
        return -1;                       /* encode_step raises ValueError: let the port do it */
    int on2 = on && !o->dry;
    if (fclamp)
        f = f < 0 ? 0 : (f > 0xFFFF ? 0xFFFF : f);
    long long fe = f & 0xFFFF;
    if (fe == 0)
        fe = 5000;
    long long duty = on2 ? (d < 0 ? 0 : (d > 255 ? 255 : d)) : 0;
    long long pw = on2 ? o->power : 0;
    unsigned long long w1 = ((unsigned long long)(fe & 0xFF) << 24) | ((unsigned long long)duty << 16)
                            | ((unsigned long long)(dy & 0xFF) << 8) | (unsigned long long)(dx & 0xFF);
    unsigned long long w2 = ((unsigned long long)(duty ? (pw & 0xFFFF) : 0) << 16)
                            | (unsigned long long)(fe >> 8);

    PyObject *idx = PyLong_FromSsize_t(PyList_GET_SIZE(o->wire));
    if (!idx)
        return -1;
    if (PyList_Append(o->starts, idx) < 0) {
        Py_DECREF(idx);
        return -1;
    }
    Py_DECREF(idx);
    PyObject *item = PyList_New(3);
    if (!item)
        return -1;
    PyObject *a = word(0, w1), *b = word(1, w2);
    if (!a || !b) {
        Py_XDECREF(a);
        Py_XDECREF(b);
        Py_DECREF(item);
        return -1;
    }
    Py_INCREF(o->header);
    PyList_SET_ITEM(item, 0, o->header);
    PyList_SET_ITEM(item, 1, a);
    PyList_SET_ITEM(item, 2, b);
    /* a wire item holds three ints and is never mutated: it cannot be part of a reference
     * cycle, so the cyclic GC need not track it (the port's lists are tracked; the difference
     * is invisible to Python code apart from gc.is_tracked) */
    PyObject_GC_UnTrack(item);
    int rc = PyList_Append(o->wire, item);
    Py_DECREF(item);
    if (rc < 0)
        return -1;
    s->steps += 1;
    s->cur0 += dx;
    s->cur1 += dy;
    if (on2 && d > 0) {
        /* self.on_len += math.hypot(dx / ppm[0], dy / ppm[1]) via CPython itself */
        double *slot = &o->hcache[(dx + 127) * 255 + (dy + 127)];
        double h = *slot;
        if (isnan(h)) {
            PyObject *r = PyObject_CallFunction(o->hyp, "LL", dx, dy);
            if (!r)
                return -1;
            if (!PyFloat_Check(r)) {
                Py_DECREF(r);
                return -1;
            }
            h = PyFloat_AS_DOUBLE(r);
            Py_DECREF(r);
            *slot = h;
        }
        s->on_len = s->on_len + h;
    }
    return 0;
}

/* Emitter.to: quantise, spread over n periods, steps */
static int do_to(Out *o, State *s, double x, double y, double ppu0, double ppu1, double max_step,
                 int on, long long d, long long f, int fclamp)
{
    /* FUN_00473bd0: v = (p * 1e4 - q * 1e4) * ppu ; t = v + carry ; d = int(t) ; carry = t - d */
    double v0 = (x * 10000.0 - s->posx * 10000.0) * ppu0;
    double t0 = v0 + s->c0;
    double v1 = (y * 10000.0 - s->posy * 10000.0) * ppu1;
    double t1 = v1 + s->c1;
    if (!(fabs(t0) < LIMIT_T) || !(fabs(t1) < LIMIT_T))
        return -1;
    long long dx = (long long)t0, dy = (long long)t1;
    if (dx > LIMIT_D || dx < -LIMIT_D || dy > LIMIT_D || dy < -LIMIT_D)
        return -1;
    s->c0 = t0 - (double)dx;
    s->c1 = t1 - (double)dy;
    s->posx = x;
    s->posy = y;
    /* n = max(1, ceil(max(abs(dx), abs(dy)) / max_step)) */
    long long m = llabs(dx) > llabs(dy) ? llabs(dx) : llabs(dy);
    double nq = ceil((double)m / max_step);
    if (!(nq <= (double)LIMIT_N))
        return -1;
    long long n = (long long)nq;
    if (n < 1)
        n = 1;
    s->overspeed += n - 1;
    long long px = 0, py = 0;
    for (long long k = 1; k <= n; k++) {
        /* ex, ey = round(dx * k / n), round(dy * k / n): |dx * k| < 2^51, exact in a double */
        long long ex = (long long)round_half_even((double)(dx * k) / (double)n);
        long long ey = (long long)round_half_even((double)(dy * k) / (double)n);
        if (do_step(o, s, ex - px, ey - py, on, d, f, fclamp) < 0)
            return -1;
        px = ex;
        py = ey;
    }
    return 0;
}

/* SpeedCurve (mlaser.nc.process) as a parsed spec, or a generic Python callable */
typedef struct {
    int native;
    int enabled;
    double thr;
    long long base;
    Py_ssize_t nseg;
    double *seg;          /* nseg * 4: a, b, y0, slope */
    PyObject *fn;         /* generic callable (borrowed) */
} Curve;

static int curve_parse(PyObject *spec, Curve *c)
{
    memset(c, 0, sizeof(*c));
    if (spec == Py_None)
        return 0;
    if (!PyTuple_Check(spec)) {
        c->fn = spec;
        return 0;
    }
    /* ("speedcurve", enabled, threshold, base, segs) */
    if (PyTuple_GET_SIZE(spec) != 5)
        return -1;
    c->native = 1;
    c->enabled = PyObject_IsTrue(PyTuple_GET_ITEM(spec, 1));
    if (c->enabled < 0) {
        PyErr_Clear();
        return -1;
    }
    if (!PyFloat_Check(PyTuple_GET_ITEM(spec, 2)))
        return -1;
    c->thr = PyFloat_AS_DOUBLE(PyTuple_GET_ITEM(spec, 2));
    if (as_ll(PyTuple_GET_ITEM(spec, 3), &c->base) < 0)
        return -1;
    PyObject *segs = PyTuple_GET_ITEM(spec, 4);
    if (!PyTuple_Check(segs))
        return -1;
    c->nseg = PyTuple_GET_SIZE(segs);
    if (c->nseg) {
        c->seg = PyMem_Malloc(sizeof(double) * 4 * (size_t)c->nseg);
        if (!c->seg) {
            PyErr_Clear();
            return -1;
        }
    }
    for (Py_ssize_t i = 0; i < c->nseg; i++) {
        PyObject *sg = PyTuple_GET_ITEM(segs, i);
        if (!PyTuple_Check(sg) || PyTuple_GET_SIZE(sg) != 4)
            return -1;
        for (int j = 0; j < 4; j++) {
            PyObject *e = PyTuple_GET_ITEM(sg, j);
            if (!PyFloat_Check(e))
                return -1;
            c->seg[i * 4 + j] = PyFloat_AS_DOUBLE(e);
        }
    }
    return 0;
}

static void curve_free(Curve *c)
{
    if (c->seg)
        PyMem_Free(c->seg);
    c->seg = NULL;
}

static int curve_eval(Curve *c, double v, long long *out)
{
    if (c->native) {
        if (!c->enabled || v > c->thr) {
            *out = c->base;
            return 0;
        }
        if (c->nseg == 0)
            return -1;            /* self.segs[0] raises IndexError */
        const double *sg = c->seg;
        for (Py_ssize_t i = 0; i < c->nseg; i++) {
            const double *s = c->seg + 4 * i;
            if (s[0] < v && v <= s[1]) {
                sg = s;
                break;
            }
        }
        /* int((v - seg[0]) * seg[3] + (seg[2] + 0.5)) */
        double r = (v - sg[0]) * sg[3] + (sg[2] + 0.5);
        if (!(fabs(r) < LIMIT_T))
            return -1;
        *out = (long long)r;
        return 0;
    }
    PyObject *arg = PyFloat_FromDouble(v);
    if (!arg)
        return -1;
    PyObject *r = PyObject_CallOneArg(c->fn, arg);
    Py_DECREF(arg);
    if (!r)
        return -1;
    int rc = as_ll(r, out);
    Py_DECREF(r);
    return rc;
}

static int parse_state(PyObject *st, State *s)
{
    if (!PyTuple_Check(st) || PyTuple_GET_SIZE(st) != 9)
        return -1;
    double *dv[5] = {&s->posx, &s->posy, &s->c0, &s->c1, &s->on_len};
    int di[5] = {0, 1, 2, 3, 8};
    for (int i = 0; i < 5; i++) {
        if (as_double(PyTuple_GET_ITEM(st, di[i]), dv[i]) < 0)
            return -1;
    }
    long long *lv[4] = {&s->cur0, &s->cur1, &s->steps, &s->overspeed};
    for (int i = 0; i < 4; i++)
        if (as_ll(PyTuple_GET_ITEM(st, 4 + i), lv[i]) < 0)
            return -1;
    return 0;
}

static PyObject *build_state(State *s)
{
    return Py_BuildValue("(ddddLLLLd)", s->posx, s->posy, s->c0, s->c1, s->cur0, s->cur1,
                         s->steps, s->overspeed, s->on_len);
}

static int parse_out(Out *o, PyObject *wire, PyObject *starts, PyObject *header, PyObject *power,
                     int dry, Py_buffer *hbuf, PyObject *hyp)
{
    if (!PyList_CheckExact(wire) || !PyList_CheckExact(starts))
        return -1;
    if (hbuf->len != (Py_ssize_t)(255 * 255 * sizeof(double)) || hbuf->readonly)
        return -1;
    o->wire = wire;
    o->starts = starts;
    o->header = header;
    o->hcache = (double *)hbuf->buf;
    o->hyp = hyp;
    o->dry = dry;
    if (as_ll(power, &o->power) < 0)
        return -1;
    return 0;
}

static PyObject *fail(Out *o, Py_ssize_t nw, Py_ssize_t ns)
{
    /* undo: the wrapper reruns the Python reference from the unchanged state */
    PyObject *et, *ev, *tb;
    PyErr_Fetch(&et, &ev, &tb);
    Py_XDECREF(et);
    Py_XDECREF(ev);
    Py_XDECREF(tb);
    if (o->wire && PyList_GET_SIZE(o->wire) > nw)
        PyList_SetSlice(o->wire, nw, PyList_GET_SIZE(o->wire), NULL);
    if (o->starts && PyList_GET_SIZE(o->starts) > ns)
        PyList_SetSlice(o->starts, ns, PyList_GET_SIZE(o->starts), NULL);
    PyErr_Clear();
    Py_RETURN_NONE;
}

/*
 * emit(state, wire, starts, header, power, dry, hcache, hyp, ppu0, ppu1, max_step,
 *      pts, ss, k_speed, total, gap, dcurve, fcurve, up, down, micro_pos, micro_int,
 *      laser, duty0, freq0, fclamp) -> new state tuple, or None (nothing changed)
 *
 * emit_samples' loop.  dcurve None: every sample uses (duty0, freq0); up/down: None or
 * (length, duty, freq) of an active override; gap: None or a list of bools; ss may be None
 * when neither curves nor overrides need it.
 */
static PyObject *k_emit(PyObject *self, PyObject *args)
{
    PyObject *st, *wire, *starts, *header, *power, *hbufo, *hyp, *pts, *ss, *gap, *dco, *fco, *up, *down;
    int dry, micro_pos, laser, fclamp;
    double ppu0, ppu1, max_step, kspd, total;
    long long micro_int, duty0, freq0;
    if (!PyArg_ParseTuple(args, "OOOOOpOOdddOOddOOOOOpLpLLp", &st, &wire, &starts, &header, &power,
                          &dry, &hbufo, &hyp, &ppu0, &ppu1, &max_step, &pts, &ss, &kspd, &total,
                          &gap, &dco, &fco, &up, &down, &micro_pos, &micro_int, &laser, &duty0,
                          &freq0, &fclamp))
        return NULL;
    State s;
    Out o;
    memset(&o, 0, sizeof(o));
    Py_buffer hbuf;
    if (PyObject_GetBuffer(hbufo, &hbuf, PyBUF_WRITABLE | PyBUF_C_CONTIGUOUS) < 0)
        return NULL;
    Py_ssize_t nw = PyList_Check(wire) ? PyList_GET_SIZE(wire) : 0;
    Py_ssize_t ns = PyList_Check(starts) ? PyList_GET_SIZE(starts) : 0;
    Curve dc, fc;
    memset(&dc, 0, sizeof(dc));
    memset(&fc, 0, sizeof(fc));
    PyObject *fp = NULL, *fs = NULL, *res = NULL;
    double up_len = 0, dn_len = 0;
    long long up_d = 0, up_f = 0, dn_d = 0, dn_f = 0;
    int has_up = up != Py_None, has_dn = down != Py_None;

    if (parse_state(st, &s) < 0 || parse_out(&o, wire, starts, header, power, dry, &hbuf, hyp) < 0)
        goto bad;
    if (!(max_step > 0.0) || !isfinite(max_step))
        goto bad;
    if (curve_parse(dco, &dc) < 0 || curve_parse(fco, &fc) < 0)
        goto bad;
    if ((dco == Py_None) != (fco == Py_None))
        goto bad;
    if (has_up) {
        if (!PyTuple_Check(up) || PyTuple_GET_SIZE(up) != 3 || as_double(PyTuple_GET_ITEM(up, 0), &up_len) < 0
            || as_ll(PyTuple_GET_ITEM(up, 1), &up_d) < 0 || as_ll(PyTuple_GET_ITEM(up, 2), &up_f) < 0)
            goto bad;
    }
    if (has_dn) {
        if (!PyTuple_Check(down) || PyTuple_GET_SIZE(down) != 3 || as_double(PyTuple_GET_ITEM(down, 0), &dn_len) < 0
            || as_ll(PyTuple_GET_ITEM(down, 1), &dn_d) < 0 || as_ll(PyTuple_GET_ITEM(down, 2), &dn_f) < 0)
            goto bad;
    }
    fp = PySequence_Fast(pts, "pts");
    if (!fp)
        goto bad;
    Py_ssize_t n = PySequence_Fast_GET_SIZE(fp);
    PyObject **pv = PySequence_Fast_ITEMS(fp);
    PyObject **sv = NULL;
    int need_ss = dco != Py_None || has_up || has_dn;
    if (need_ss) {
        fs = PySequence_Fast(ss, "ss");
        if (!fs || PySequence_Fast_GET_SIZE(fs) != n)
            goto bad;
        sv = PySequence_Fast_ITEMS(fs);
    }
    if (gap != Py_None && (!PyList_CheckExact(gap) || PyList_GET_SIZE(gap) != n))
        goto bad;

    for (Py_ssize_t i = 0; i < n; i++) {
        double si = 0.0;
        long long duty = duty0, freq = freq0;
        if (need_ss && as_double(sv[i], &si) < 0)
            goto bad;
        if (dco != Py_None) {
            /* sample_speeds: sp[i] = (ss[i] - ss[i-1]) * k, sp[0] = sp[1] */
            double sp = 0.0, a, b;
            if (n > 1) {
                Py_ssize_t j = i == 0 ? 1 : i;
                if (as_double(sv[j], &b) < 0 || as_double(sv[j - 1], &a) < 0)
                    goto bad;
                sp = (b - a) * kspd;
            }
            if (curve_eval(&dc, sp, &duty) < 0 || curve_eval(&fc, sp, &freq) < 0)
                goto bad;
        }
        if (has_up && si < up_len) {
            duty = up_d;
            freq = up_f;
        }
        if (has_dn && total - si < dn_len) {
            duty = dn_d;
            freq = dn_f;
        }
        int on = laser;
        if (gap != Py_None) {
            int g = PyObject_IsTrue(PyList_GET_ITEM(gap, i));
            if (g < 0)
                goto bad;
            if (g) {
                if (laser && micro_pos) {
                    if (duty > LIMIT_D || duty < -LIMIT_D || micro_int > LIMIT_D || micro_int < -LIMIT_D)
                        goto bad;
                    duty = floordiv(duty * micro_int, 100);   /* int(duty) * int(pct) // 100 */
                } else {
                    on = 0;
                }
            }
        }
        PyObject *p = pv[i];
        double x, y;
        if (!PyTuple_Check(p) || PyTuple_GET_SIZE(p) != 2 || as_double(PyTuple_GET_ITEM(p, 0), &x) < 0
            || as_double(PyTuple_GET_ITEM(p, 1), &y) < 0)
            goto bad;
        if (do_to(&o, &s, x, y, ppu0, ppu1, max_step, on, duty, freq, fclamp) < 0)
            goto bad;
    }
    res = build_state(&s);
    if (!res)
        goto bad;
    Py_XDECREF(fp);
    Py_XDECREF(fs);
    curve_free(&dc);
    curve_free(&fc);
    PyBuffer_Release(&hbuf);
    return res;
bad:
    Py_XDECREF(fp);
    Py_XDECREF(fs);
    curve_free(&dc);
    curve_free(&fc);
    PyBuffer_Release(&hbuf);
    return fail(&o, nw, ns);
}

/*
 * steps(state, wire, starts, header, power, dry, hcache, hyp, count, on, duty, freq)
 * -> new state or None: ``count`` zero steps (Emitter.dwell) with the emitter's own duty and
 * frequency (no clamp of freq).
 */
static PyObject *k_steps(PyObject *self, PyObject *args)
{
    PyObject *st, *wire, *starts, *header, *power, *hbufo, *hyp;
    int dry, on;
    long long count, duty, freq;
    if (!PyArg_ParseTuple(args, "OOOOOpOOLpLL", &st, &wire, &starts, &header, &power, &dry, &hbufo,
                          &hyp, &count, &on, &duty, &freq))
        return NULL;
    State s;
    Out o;
    memset(&o, 0, sizeof(o));
    Py_buffer hbuf;
    if (PyObject_GetBuffer(hbufo, &hbuf, PyBUF_WRITABLE | PyBUF_C_CONTIGUOUS) < 0)
        return NULL;
    Py_ssize_t nw = PyList_Check(wire) ? PyList_GET_SIZE(wire) : 0;
    Py_ssize_t ns = PyList_Check(starts) ? PyList_GET_SIZE(starts) : 0;
    PyObject *res;
    if (parse_state(st, &s) < 0 || parse_out(&o, wire, starts, header, power, dry, &hbuf, hyp) < 0)
        goto bad;
    for (long long i = 0; i < count; i++)
        if (do_step(&o, &s, 0, 0, on, duty, freq, 0) < 0)
            goto bad;
    res = build_state(&s);
    if (!res)
        goto bad;
    PyBuffer_Release(&hbuf);
    return res;
bad:
    PyBuffer_Release(&hbuf);
    return fail(&o, nw, ns);
}

/* round_half_even and the truncation exposed for the property tests */
static PyObject *k_round(PyObject *self, PyObject *arg)
{
    double q = PyFloat_AsDouble(arg);
    if (q == -1.0 && PyErr_Occurred())
        return NULL;
    return PyFloat_FromDouble(round_half_even(q));
}

static PyMethodDef methods[] = {
    {"emit", k_emit, METH_VARARGS, "emit_samples loop (see _emitter.c)"},
    {"steps", k_steps, METH_VARARGS, "zero steps (Emitter.dwell)"},
    {"round_half_even", k_round, METH_O, "round-half-even of a double (test hook)"},
    {NULL, NULL, 0, NULL},
};

/* the word cache is process-global: no per-interpreter-GIL subinterpreters, and the GIL is
 * required (no Py_mod_gil slot: a free-threaded build re-enables the GIL on import) */
static PyModuleDef_Slot slots[] = {
    {0, NULL},
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT, "_emitter", "mlaser native emitter kernel", 0, methods, slots,
    NULL, NULL, NULL,
};

PyMODINIT_FUNC PyInit__emitter(void)
{
    return PyModuleDef_Init(&module);
}
