/* mlaser._native: declarations shared by _emitter.c, _geom.c and _ring.c (one extension
 * module, docs/dev/NATIVE_KERNEL.md).  Licence: MIT (same as mlaser). */
#ifndef MLASER_NATIVE_GEOM_H
#define MLASER_NATIVE_GEOM_H

#include <Python.h>

/* one record of the CurveTable sample table (curvetable._Rec) */
typedef struct {
    double u, x, y, s, f, tx, ty, kx, ky, R, len;
    int arc;
    double cx, cy, rad, a0, a1;
} Rec;

typedef struct {
    PyObject_HEAD
    Rec *r;
    Py_ssize_t n;
    double total;
    int mono;               /* s never decreases: binary search == the port's linear scan */
    double *ctrl;           /* control points x0, y0, x1, ... (point_f before the first record) */
    Py_ssize_t nctrl;
} CTable;

double mlaser_crt_acos(double x);
double mlaser_crt_atan(double x);
double mlaser_crt_atan2(double y, double x);
double mlaser_crt_hypot(double x, double y);
int mlaser_ct_point_at_fraction(const CTable *t, double tf, double *x, double *y);
int mlaser_ct_point(const CTable *t, double u0, double u1, double length, double s, double *x, double *y);

/* module exec parts: add the types / functions of _geom.c and _ring.c to the module */
int mlaser_geom_exec(PyObject *m);
int mlaser_ring_exec(PyObject *m);

#endif
