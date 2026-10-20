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

#include "_geom.h"

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

/* ------------------------------------------------------------------------------------------
 * Block: a malloc'ed byte buffer exported through the buffer protocol (numpy.frombuffer views
 * it without a copy); Sink: the packed wire storage of mlaser.nc.packed.WireBuilder in C.
 *
 * The Sink holds exactly what WireBuilder holds (nc/packed.py): the item words concatenated,
 * the word offset of every run (woff, starting with 0), the run counts (cnt) and the group
 * start flag of every run (flag); consecutive equal items with the same flag are one run.
 * Words are stored as uint32 while every word lies in 0 .. 2^32 - 1 and as int64 after the
 * first one that does not (WireBuilder keeps int64 and narrows to uint32 at the end: same
 * arrays).  The kernel appends its step items here directly: no Python list per step.
 * ------------------------------------------------------------------------------------------ */
typedef struct {
    PyObject_HEAD
    char *buf;
    Py_ssize_t len;
    Py_ssize_t exports;
} Block;

static void block_dealloc(Block *b)
{
    PyMem_Free(b->buf);
    Py_TYPE(b)->tp_free((PyObject *)b);
}

static int block_getbuffer(Block *b, Py_buffer *view, int flags)
{
    if (PyBuffer_FillInfo(view, (PyObject *)b, b->buf ? b->buf : (char *)"", b->len, 0, flags) < 0)
        return -1;
    b->exports++;
    return 0;
}

static void block_releasebuffer(Block *b, Py_buffer *view)
{
    b->exports--;
}

static Py_ssize_t block_len(Block *b)
{
    return b->len;
}

static PyBufferProcs block_as_buffer = {(getbufferproc)block_getbuffer, (releasebufferproc)block_releasebuffer};
static PySequenceMethods block_as_seq = {(lenfunc)block_len};

static PyTypeObject BlockType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "mlaser._native._emitter.Block",
    .tp_basicsize = sizeof(Block),
    .tp_dealloc = (destructor)block_dealloc,
    .tp_as_buffer = &block_as_buffer,
    .tp_as_sequence = &block_as_seq,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = "read-only byte block of a packed wire array (buffer protocol)",
};

/* a growable array of fixed-size elements */
typedef struct {
    char *buf;
    Py_ssize_t n, cap;      /* in elements */
    Py_ssize_t esize;
} Vec;

static int vec_reserve(Vec *v, Py_ssize_t need)
{
    if (need <= v->cap)
        return 0;
    Py_ssize_t cap = v->cap < 16 ? 16 : v->cap;
    while (cap < need)
        cap = cap + (cap >> 1);
    if (cap > PY_SSIZE_T_MAX / v->esize) {
        PyErr_NoMemory();
        return -1;
    }
    char *nb = PyMem_Realloc(v->buf, (size_t)(cap * v->esize));
    if (!nb) {
        PyErr_NoMemory();
        return -1;
    }
    v->buf = nb;
    v->cap = cap;
    return 0;
}

/* the vector's buffer as a Block of exactly its length (the vector is emptied) */
static PyObject *vec_to_block(Vec *v)
{
    Block *b = PyObject_New(Block, &BlockType);
    if (!b)
        return NULL;
    Py_ssize_t len = v->n * v->esize;
    char *buf = v->buf;
    if (buf && len < v->cap * v->esize) {
        char *shrunk = PyMem_Realloc(buf, len ? (size_t)len : 1);
        if (shrunk)
            buf = shrunk;
    }
    b->buf = buf;
    b->len = len;
    b->exports = 0;
    v->buf = NULL;
    v->n = v->cap = 0;
    return (PyObject *)b;
}

typedef struct {
    PyObject_HEAD
    Vec words;              /* uint32 (wide == 0) or int64 (wide == 1) */
    Vec woff;               /* int64, woff[0] = 0 */
    Vec cnt;                /* int64 */
    Vec flag;               /* uint8 */
    Py_ssize_t n;           /* items */
    int wide, pending, finished;
} Sink;

typedef struct {
    Py_ssize_t nwords, nruns, n;
    long long lastcnt;
    int pending;
} SinkMark;

#define SINK_RUNS(k) ((k)->cnt.n)
#define SINK_W32(k) ((unsigned int *)(k)->words.buf)
#define SINK_W64(k) ((long long *)(k)->words.buf)
#define SINK_WOFF(k) ((long long *)(k)->woff.buf)
#define SINK_CNT(k) ((long long *)(k)->cnt.buf)

static int sink_widen(Sink *k)
{
    Py_ssize_t n = k->words.n;
    Vec w = {NULL, 0, 0, 8};
    if (vec_reserve(&w, n + 16) < 0)
        return -1;
    unsigned int *src = SINK_W32(k);
    long long *dst = (long long *)w.buf;
    for (Py_ssize_t i = 0; i < n; i++)
        dst[i] = (long long)src[i];
    w.n = n;
    PyMem_Free(k->words.buf);
    k->words = w;
    k->wide = 1;
    return 0;
}

static long long sink_word(Sink *k, Py_ssize_t i)
{
    return k->wide ? SINK_W64(k)[i] : (long long)SINK_W32(k)[i];
}

/* append one item of m words (WireBuilder.append): merged into the last run when equal */
static int sink_put(Sink *k, const long long *w, Py_ssize_t m)
{
    int flag = k->pending;
    k->pending = 0;
    Py_ssize_t r = SINK_RUNS(k);
    if (r > 0 && ((unsigned char *)k->flag.buf)[r - 1] == (unsigned char)flag) {
        long long a = SINK_WOFF(k)[r - 1], b = SINK_WOFF(k)[r];
        if (b - a == m) {
            Py_ssize_t j = 0;
            while (j < m && sink_word(k, (Py_ssize_t)a + j) == w[j])
                j++;
            if (j == m) {
                SINK_CNT(k)[r - 1] += 1;
                k->n += 1;
                return 0;
            }
        }
    }
    if (!k->wide) {
        for (Py_ssize_t j = 0; j < m; j++)
            if (w[j] < 0 || w[j] > 0xFFFFFFFFLL) {
                if (sink_widen(k) < 0)
                    return -1;
                break;
            }
    }
    if (vec_reserve(&k->words, k->words.n + m) < 0 || vec_reserve(&k->woff, k->woff.n + 1) < 0
        || vec_reserve(&k->cnt, k->cnt.n + 1) < 0 || vec_reserve(&k->flag, k->flag.n + 1) < 0)
        return -1;
    if (k->wide) {
        long long *d = SINK_W64(k) + k->words.n;
        for (Py_ssize_t j = 0; j < m; j++)
            d[j] = w[j];
    } else {
        unsigned int *d = SINK_W32(k) + k->words.n;
        for (Py_ssize_t j = 0; j < m; j++)
            d[j] = (unsigned int)w[j];
    }
    k->words.n += m;
    SINK_WOFF(k)[k->woff.n++] = (long long)k->words.n;
    SINK_CNT(k)[k->cnt.n++] = 1;
    ((unsigned char *)k->flag.buf)[k->flag.n++] = (unsigned char)flag;
    k->n += 1;
    return 0;
}

static void sink_mark(Sink *k, SinkMark *mk)
{
    mk->nwords = k->words.n;
    mk->nruns = SINK_RUNS(k);
    mk->n = k->n;
    mk->pending = k->pending;
    mk->lastcnt = mk->nruns ? SINK_CNT(k)[mk->nruns - 1] : 0;
}

static void sink_restore(Sink *k, const SinkMark *mk)
{
    k->words.n = mk->nwords;
    k->cnt.n = mk->nruns;
    k->flag.n = mk->nruns;
    k->woff.n = mk->nruns + 1;
    k->n = mk->n;
    k->pending = mk->pending;
    if (mk->nruns)
        SINK_CNT(k)[mk->nruns - 1] = mk->lastcnt;
}

static int sink_init(Sink *k, PyObject *args, PyObject *kw)
{
    static char *kwlist[] = {NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kw, "", kwlist))
        return -1;
    PyMem_Free(k->words.buf);
    PyMem_Free(k->woff.buf);
    PyMem_Free(k->cnt.buf);
    PyMem_Free(k->flag.buf);
    k->words = (Vec){NULL, 0, 0, 4};
    k->woff = (Vec){NULL, 0, 0, 8};
    k->cnt = (Vec){NULL, 0, 0, 8};
    k->flag = (Vec){NULL, 0, 0, 1};
    k->n = 0;
    k->wide = k->pending = k->finished = 0;
    if (vec_reserve(&k->woff, 16) < 0)
        return -1;
    SINK_WOFF(k)[0] = 0;
    k->woff.n = 1;
    return 0;
}

static void sink_dealloc(Sink *k)
{
    PyMem_Free(k->words.buf);
    PyMem_Free(k->woff.buf);
    PyMem_Free(k->cnt.buf);
    PyMem_Free(k->flag.buf);
    Py_TYPE(k)->tp_free((PyObject *)k);
}

static int sink_open(Sink *k)
{
    if (k->finished || !k->woff.buf) {
        PyErr_SetString(PyExc_RuntimeError, "the wire sink is finished");
        return -1;
    }
    return 0;
}

static PyObject *sink_append(Sink *k, PyObject *item)
{
    if (sink_open(k) < 0)
        return NULL;
    PyObject *f = PySequence_Fast(item, "a wire item must be a sequence of ints");
    if (!f)
        return NULL;
    Py_ssize_t m = PySequence_Fast_GET_SIZE(f);
    long long stackw[8];
    long long *w = m <= 8 ? stackw : PyMem_Malloc(sizeof(long long) * (size_t)m);
    if (!w) {
        Py_DECREF(f);
        return PyErr_NoMemory();
    }
    PyObject **it = PySequence_Fast_ITEMS(f);
    for (Py_ssize_t j = 0; j < m; j++) {
        /* array('q').extend: an int (or __index__) of 64 bits, else TypeError / OverflowError */
        PyObject *o = it[j];
        if (PyFloat_Check(o) || !PyIndex_Check(o)) {
            PyErr_SetString(PyExc_TypeError, "a wire word must be an int");
            goto bad;
        }
        long long v = PyLong_AsLongLong(o);
        if (v == -1 && PyErr_Occurred())
            goto bad;
        w[j] = v;
    }
    if (sink_put(k, w, m) < 0)
        goto bad;
    if (w != stackw)
        PyMem_Free(w);
    Py_DECREF(f);
    Py_RETURN_NONE;
bad:
    if (w != stackw)
        PyMem_Free(w);
    Py_DECREF(f);
    return NULL;
}

static PyObject *sink_mark_start(Sink *k, PyObject *arg)
{
    Py_ssize_t i = PyNumber_AsSsize_t(arg, PyExc_OverflowError);
    if (i == -1 && PyErr_Occurred())
        return NULL;
    if (i == k->n)
        k->pending = 1;
    Py_RETURN_NONE;
}

/* finish() -> (words, wide, woff, cnt, flag) Blocks; the sink is then closed.  An int64
 * word vector whose words all fit in uint32 is narrowed (as WireBuilder narrows). */
static PyObject *sink_finish(Sink *k, PyObject *unused)
{
    if (sink_open(k) < 0)
        return NULL;
    if (k->wide) {
        int fits = 1;
        for (Py_ssize_t i = 0; i < k->words.n && fits; i++) {
            long long v = SINK_W64(k)[i];
            fits = v >= 0 && v <= 0xFFFFFFFFLL;
        }
        if (fits) {
            Vec w = {NULL, 0, 0, 4};
            if (vec_reserve(&w, k->words.n + 1) < 0)
                return NULL;
            for (Py_ssize_t i = 0; i < k->words.n; i++)
                ((unsigned int *)w.buf)[i] = (unsigned int)SINK_W64(k)[i];
            w.n = k->words.n;
            PyMem_Free(k->words.buf);
            k->words = w;
            k->wide = 0;
        }
    }
    PyObject *a = vec_to_block(&k->words), *b = vec_to_block(&k->woff), *c = vec_to_block(&k->cnt),
             *d = vec_to_block(&k->flag);
    k->finished = 1;
    if (!a || !b || !c || !d) {
        Py_XDECREF(a);
        Py_XDECREF(b);
        Py_XDECREF(c);
        Py_XDECREF(d);
        return NULL;
    }
    return Py_BuildValue("(NiNNN)", a, k->wide, b, c, d);
}

static Py_ssize_t sink_len(Sink *k)
{
    return k->n;
}

static PyObject *sink_get_pending(Sink *k, void *c)
{
    return PyBool_FromLong(k->pending);
}

static PyMethodDef sink_methods[] = {
    {"append", (PyCFunction)sink_append, METH_O, "append one wire item (a sequence of ints)"},
    {"mark_start", (PyCFunction)sink_mark_start, METH_O, "the next item starts a group when index == len"},
    {"finish", (PyCFunction)sink_finish, METH_NOARGS, "(words, wide, woff, cnt, flag) blocks; closes the sink"},
    {NULL, NULL, 0, NULL},
};

static PyGetSetDef sink_getset[] = {
    {"pending", (getter)sink_get_pending, NULL, "a group start is pending", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PySequenceMethods sink_as_seq = {(lenfunc)sink_len};

static PyTypeObject SinkType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "mlaser._native._emitter.Sink",
    .tp_basicsize = sizeof(Sink),
    .tp_dealloc = (destructor)sink_dealloc,
    .tp_as_sequence = &sink_as_seq,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = "packed wire storage (nc/packed.py WireBuilder) written by the kernel directly",
    .tp_methods = sink_methods,
    .tp_getset = sink_getset,
    .tp_init = (initproc)sink_init,
    .tp_new = PyType_GenericNew,
};

typedef struct {
    PyObject *wire, *starts, *header;   /* borrowed */
    Sink *sink;                           /* wire is a Sink (starts unused), else NULL */
    long long header_v;
    SinkMark mark;
    double *hcache;                       /* 255 x 255, NaN = not computed */
    PyObject *hyp;                        /* callable (dx, dy) -> float, borrowed */
    long long power;
    int dry;
} Out;

/* round-half-even of a double, as Python's round(x) for |x| < 2^52.
 * The parity of r is an integer test, not fmod(r, 2.0): LLVM 20+ (Apple clang 21) lowers fmod
 * by a constant power of two to x - trunc(x / 2) * 2 with an fmadd/fmsub even under
 * -ffp-contract=off (exact, but it is a fused multiply-add the FMA check must refuse).
 * diff == 0.5 means q is not an integer, so |r| < 2^52 and (long long)r is exact. */
static double round_half_even(double q)
{
    double r = floor(q);
    double diff = q - r;        /* exact */
    if (diff > 0.5)
        r += 1.0;
    else if (diff == 0.5 && ((long long)r & 1LL) != 0)
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

/* a C-contiguous buffer of doubles (array('d'), numpy float64); lists and tuples are not
 * buffers.  0 and the view filled, or -1 (no error set) */
static int dbuf(PyObject *o, Py_buffer *view)
{
    view->obj = NULL;
    if (PyList_Check(o) || PyTuple_Check(o) || !PyObject_CheckBuffer(o))
        return -1;
    if (PyObject_GetBuffer(o, view, PyBUF_FORMAT | PyBUF_C_CONTIGUOUS) < 0) {
        PyErr_Clear();
        view->obj = NULL;
        return -1;
    }
    const char *f = view->format ? view->format : "B";
    if (*f == '<' || *f == '=' || *f == '@')
        f++;
    if (view->itemsize != 8 || strcmp(f, "d") != 0) {
        PyBuffer_Release(view);
        view->obj = NULL;
        return -1;
    }
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

    if (o->sink) {
        /* starts.append(len(wire)) + wire.append([header, w1, w2]) of the packed builder */
        long long it[3] = {o->header_v, (long long)w1, (long long)w2};
        o->sink->pending = 1;
        if (sink_put(o->sink, it, 3) < 0)
            return -1;
        goto counted;
    }
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
counted:
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
    if (Py_IS_TYPE(wire, &SinkType)) {
        o->sink = (Sink *)wire;
        if (o->sink->finished || !o->sink->woff.buf)
            return -1;
        if (as_ll(header, &o->header_v) < 0)
            return -1;
        sink_mark(o->sink, &o->mark);
    } else if (!PyList_CheckExact(wire) || !PyList_CheckExact(starts)) {
        return -1;
    }
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
    if (o->sink) {
        sink_restore(o->sink, &o->mark);
        PyErr_Clear();
        Py_RETURN_NONE;
    }
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
    Py_buffer pbuf, sbuf;
    pbuf.obj = sbuf.obj = NULL;
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
    /* pts: a sequence of (x, y) tuples, or a flat buffer of doubles x0, y0, x1, y1, ...
     * (array('d') from a native geometry); ss: a sequence of floats or a buffer of doubles */
    Py_ssize_t n;
    PyObject **pv = NULL;
    const double *pd = NULL, *sd = NULL;
    if (dbuf(pts, &pbuf) == 0) {
        if (pbuf.len % 16)
            goto bad;
        n = pbuf.len / 16;
        pd = (const double *)pbuf.buf;
    } else {
        fp = PySequence_Fast(pts, "pts");
        if (!fp)
            goto bad;
        n = PySequence_Fast_GET_SIZE(fp);
        pv = PySequence_Fast_ITEMS(fp);
    }
    PyObject **sv = NULL;
    int need_ss = dco != Py_None || has_up || has_dn;
    if (need_ss) {
        if (dbuf(ss, &sbuf) == 0) {
            if (sbuf.len != n * 8)
                goto bad;
            sd = (const double *)sbuf.buf;
        } else {
            fs = PySequence_Fast(ss, "ss");
            if (!fs || PySequence_Fast_GET_SIZE(fs) != n)
                goto bad;
            sv = PySequence_Fast_ITEMS(fs);
        }
    }
    if (gap != Py_None && (!PyList_CheckExact(gap) || PyList_GET_SIZE(gap) != n))
        goto bad;

    for (Py_ssize_t i = 0; i < n; i++) {
        double si = 0.0;
        long long duty = duty0, freq = freq0;
        if (need_ss) {
            if (sd)
                si = sd[i];
            else if (as_double(sv[i], &si) < 0)
                goto bad;
        }
        if (dco != Py_None) {
            /* sample_speeds: sp[i] = (ss[i] - ss[i-1]) * k, sp[0] = sp[1] */
            double sp = 0.0, a, b;
            if (n > 1) {
                Py_ssize_t j = i == 0 ? 1 : i;
                if (sd) {
                    b = sd[j];
                    a = sd[j - 1];
                } else if (as_double(sv[j], &b) < 0 || as_double(sv[j - 1], &a) < 0) {
                    goto bad;
                }
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
        double x, y;
        if (pd) {
            x = pd[2 * i];
            y = pd[2 * i + 1];
        } else {
            PyObject *p = pv[i];
            if (!PyTuple_Check(p) || PyTuple_GET_SIZE(p) != 2 || as_double(PyTuple_GET_ITEM(p, 0), &x) < 0
                || as_double(PyTuple_GET_ITEM(p, 1), &y) < 0)
                goto bad;
        }
        if (do_to(&o, &s, x, y, ppu0, ppu1, max_step, on, duty, freq, fclamp) < 0)
            goto bad;
    }
    res = build_state(&s);
    if (!res)
        goto bad;
    if (pbuf.obj)
        PyBuffer_Release(&pbuf);
    if (sbuf.obj)
        PyBuffer_Release(&sbuf);
    Py_XDECREF(fp);
    Py_XDECREF(fs);
    curve_free(&dc);
    curve_free(&fc);
    PyBuffer_Release(&hbuf);
    return res;
bad:
    if (pbuf.obj)
        PyBuffer_Release(&pbuf);
    if (sbuf.obj)
        PyBuffer_Release(&sbuf);
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
/* mlaser._native.KERNEL_VERSION of the sources this module was built from: a stale build
 * (an editable install not rebuilt) is refused by the selection */
#define MLASER_KERNEL_VERSION 3

static int module_exec(PyObject *m)
{
    if (PyModule_AddIntConstant(m, "KERNEL_VERSION", MLASER_KERNEL_VERSION) < 0)
        return -1;
    if (mlaser_geom_exec(m) < 0)
        return -1;
    if (PyType_Ready(&BlockType) < 0 || PyType_Ready(&SinkType) < 0)
        return -1;
    Py_INCREF(&SinkType);
    if (PyModule_AddObject(m, "Sink", (PyObject *)&SinkType) < 0) {
        Py_DECREF(&SinkType);
        return -1;
    }
    Py_INCREF(&BlockType);
    if (PyModule_AddObject(m, "Block", (PyObject *)&BlockType) < 0) {
        Py_DECREF(&BlockType);
        return -1;
    }
    return 0;
}

static PyModuleDef_Slot slots[] = {
    {Py_mod_exec, (void *)module_exec},
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
