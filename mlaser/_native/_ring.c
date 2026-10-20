/*
 * mlaser._native._emitter, ring sort part: mlaser.parity_ports.cadmodule.ordering.ring_sort
 * (CRingSort 0x1003f690, sort modes 6 / 7) in C, with the same picks in the same order.
 *
 * The reference walks a gift-wrap spiral: for every pick it scans all remaining nodes from the
 * second-to-last one down to the first and keeps the last candidate that turns clockwise of the
 * current direction v by more than asin(0.1), or lies nearly on it and closer, unless a candidate
 * inside the tolerance box of the current point ends the scan; then ``list.remove``.  The scan
 * is inherently sequential: on a nested sheet the direction is replaced about a hundred times
 * per pick (2 500 nodes, measured), each decision depending on the one before, so this file
 * keeps the scan (O(n) per pick) and makes each candidate cheap:
 *
 *   - the tolerance box test, the cross product and the comparisons are the reference's own
 *     double operations;
 *   - |w| and |v| are CPython's math.hypot in the reference (CPython's own algorithm, within
 *     1 ulp).  Here sqrt(x*x + y*y) (within 1.5 ulp of the exact value, outside 1e-150 ..
 *     1e150) is used when every decision it feeds is more than 1e-13 away from its threshold
 *     (a few ulp move a decision by about 1e-15); otherwise CPython's math.hypot of both is
 *     called back and the candidate is decided with those values;
 *   - removal from an int array (memmove), not a Python list.
 *
 * ``exact=True`` calls math.hypot back for every candidate (the reference arithmetic
 * throughout; the tests compare both).  Licence: MIT (same as mlaser).
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
#if defined(__clang__)
#pragma STDC FP_CONTRACT OFF
#endif
#if defined(_MSC_VER)
#pragma float_control(precise, on)
#pragma fp_contract(off)
#endif

#include "_geom.h"

/* an approximation of |(x, y)| within 2 ulp of the exact value (sqrt of the rounded sum of the
 * rounded squares: <= 1.5 ulp), or -1.0 where the squares could overflow / underflow (the caller
 * then takes the exact path) */
static double approx_norm(double x, double y)
{
    double ax = fabs(x), ay = fabs(y);
    double m = ax > ay ? ax : ay;
    if (!(m < 1e150) || (m != 0.0 && m < 1e-150))
        return -1.0;
    return sqrt(x * x + y * y);
}

#define MARGIN 1e-13

/* CPython's math.hypot(x, y) */
static int py_hypot(PyObject *fn, double x, double y, double *out)
{
    PyObject *r = PyObject_CallFunction(fn, "dd", x, y);
    if (!r)
        return -1;
    if (!PyFloat_Check(r)) {
        Py_DECREF(r);
        PyErr_SetString(PyExc_TypeError, "hypot must return a float");
        return -1;
    }
    *out = PyFloat_AS_DOUBLE(r);
    Py_DECREF(r);
    return 0;
}

/* the reference's decision for candidate w with |w| = nw: 0 skip (continue), 1 update */
static int decide(double v0, double v1, double nv, double w0, double w1, double nw)
{
    if (nw < 1e-12 || nv < 1e-12)
        return 0;
    double sn = (v0 * w1 - v1 * w0) / (nw * nv);
    if (sn <= -0.1)
        return 1;
    if (fabs(sn) < 0.1 && nw < (1 - 4 * fabs(sn)) * nv)
        return 1;
    return 0;
}

/* the same with approximate |v| and |w| (libm's, each within 2 ulp of CPython's): 0 / 1, or -1
 * when a decision is too close to call */
static int decide_approx(double v0, double v1, double nv, double w0, double w1, double nw)
{
    if (nv < 1e-12 * (1.0 - 1e-12))
        return 0;
    if (!(nv > 1e-12 * (1.0 + 1e-12)))
        return -1;
    if (nw < 1e-12 * (1.0 - 1e-12))
        return 0;
    if (!(nw > 1e-12 * (1.0 + 1e-12)))
        return -1;                                  /* also NaN: the exact path */
    double sn = (v0 * w1 - v1 * w0) / (nw * nv);
    if (!isfinite(sn))
        return -1;
    if (sn < -0.1 - MARGIN)
        return 1;
    if (!(sn > -0.1 + MARGIN))
        return -1;
    double a = fabs(sn);
    if (a > 0.1 + MARGIN)
        return 0;
    if (!(a < 0.1 - MARGIN))
        return -1;
    double rhs = (1 - 4 * a) * nv;
    double slack = (4.0 * MARGIN + 1e-14) * nv;
    if (nw < rhs - slack)
        return 1;
    if (nw > rhs + slack)
        return 0;
    return -1;
}

/*
 * ring_order(P, tol, rx, ry, hypot, exact) -> list of the picked indexes (the reference's
 * ``out``), P a buffer of 2n doubles x0, y0, x1, y1, ... (the bbox centres, computed by the
 * Python wrapper as the reference computes them).
 */
static PyObject *g_ring_order(PyObject *self, PyObject *args)
{
    PyObject *po, *hyp;
    double tol, rx, ry;
    int exact;
    if (!PyArg_ParseTuple(args, "OdddOp", &po, &tol, &rx, &ry, &hyp, &exact))
        return NULL;
    Py_buffer pb;
    if (PyObject_GetBuffer(po, &pb, PyBUF_FORMAT | PyBUF_C_CONTIGUOUS) < 0)
        return NULL;
    const char *f = pb.format ? pb.format : "B";
    if (*f == '<' || *f == '=' || *f == '@')
        f++;
    if (pb.itemsize != 8 || strcmp(f, "d") != 0 || pb.len % 16) {
        PyBuffer_Release(&pb);
        PyErr_SetString(PyExc_TypeError, "P must be a buffer of 2n doubles");
        return NULL;
    }
    Py_ssize_t n = pb.len / 16;
    const double *xy = (const double *)pb.buf;
    Py_ssize_t *idx = PyMem_Malloc(sizeof(Py_ssize_t) * (size_t)(n + 1));
    PyObject *out = PyList_New(0);
    if (!idx || !out) {
        PyMem_Free(idx);
        Py_XDECREF(out);
        PyBuffer_Release(&pb);
        return PyErr_NoMemory();
    }
    for (Py_ssize_t i = 0; i < n; i++)
        idx[i] = i;
    Py_ssize_t m = n;
    double R0 = rx, R1 = ry;
    while (m > 0) {
        Py_ssize_t last = idx[m - 1];
        double v0 = xy[2 * last] - R0, v1 = xy[2 * last + 1] - R1;
        Py_ssize_t pick = last, pick_ii = m - 1;
        if (!(fabs(v0) < tol && fabs(v1) < tol)) {
            /* |v|: libm's (nva) for the quick decisions, CPython's (nv) fetched when a
             * candidate needs the exact arithmetic */
            double nv = 0.0, nva;
            int nv_ok = 0;
            if (exact) {
                if (py_hypot(hyp, v0, v1, &nv) < 0)
                    goto error;
                nv_ok = 1;
                nva = nv;
            } else {
                nva = approx_norm(v0, v1);
            }
            for (Py_ssize_t ii = m - 2; ii >= 0; ii--) {
                Py_ssize_t i = idx[ii];
                double w0 = xy[2 * i] - R0, w1 = xy[2 * i + 1] - R1;
                if (fabs(w0) < tol && fabs(w1) < tol) {
                    pick = i;
                    pick_ii = ii;
                    break;
                }
                double nwa = 0.0;
                int upd = -1;
                if (!exact) {
                    nwa = approx_norm(w0, w1);
                    upd = (nva < 0.0 || nwa < 0.0) ? -1 : decide_approx(v0, v1, nva, w0, w1, nwa);
                }
                if (upd < 0) {
                    double nw;
                    if (!nv_ok) {
                        if (py_hypot(hyp, v0, v1, &nv) < 0)
                            goto error;
                        nv_ok = 1;
                    }
                    if (py_hypot(hyp, w0, w1, &nw) < 0)
                        goto error;
                    upd = decide(v0, v1, nv, w0, w1, nw);
                    if (upd) {
                        nv = nva = nw;        /* math.hypot(*v) of the new v */
                        nv_ok = 1;
                    }
                } else if (upd) {
                    nva = nwa;
                    nv_ok = 0;
                }
                if (upd) {
                    v0 = w0;
                    v1 = w1;
                    pick = i;
                    pick_ii = ii;
                    if (fabs(v0) < tol && fabs(v1) < tol)
                        break;
                }
            }
        }
        PyObject *o = PyLong_FromSsize_t(pick);
        if (!o || PyList_Append(out, o) < 0) {
            Py_XDECREF(o);
            goto error;
        }
        Py_DECREF(o);
        memmove(idx + pick_ii, idx + pick_ii + 1, sizeof(Py_ssize_t) * (size_t)(m - pick_ii - 1));
        m -= 1;
        R0 = xy[2 * pick];
        R1 = xy[2 * pick + 1];
    }
    PyMem_Free(idx);
    PyBuffer_Release(&pb);
    return out;
error:
    PyMem_Free(idx);
    Py_XDECREF(out);
    PyBuffer_Release(&pb);
    return NULL;
}

static PyMethodDef ring_methods[] = {
    {"ring_order", g_ring_order, METH_VARARGS,
     "ring_order(P, tol, rx, ry, hypot, exact) -> picked indexes of ordering.ring_sort"},
    {NULL, NULL, 0, NULL},
};

int mlaser_ring_exec(PyObject *m)
{
    return PyModule_AddFunctions(m, ring_methods);
}
